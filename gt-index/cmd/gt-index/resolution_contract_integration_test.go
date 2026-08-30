package main

import (
	"database/sql"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	_ "github.com/mattn/go-sqlite3"
)

// TestResolutionContractIsWrittenByTheRealCLI exercises the producer boundary,
// rather than only testing the resolver/store helpers in isolation. The
// artifact must contain revision-bound callsites and candidates whose target
// identities are foreign-keyed to producer-emitted symbols.
func TestResolutionContractIsWrittenByTheRealCLI(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, "mod.py"), []byte("def target(value):\n    return value + 1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, "caller.py"), []byte("from mod import target\n\ndef local_target(value):\n    return value\n\ndef caller(value):\n    local_target(value)\n    return target(value)\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, "dynamic.js"), []byte("function choose(name) { return handlers[name](); }\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	bin := filepath.Join(tmp, "gt-index")
	if runtime.GOOS == "windows" {
		bin += ".exe"
	}
	build := exec.Command("go", "build", "-tags", "sqlite_fts5", "-ldflags", testBuildLDFlags, "-o", bin, ".")
	build.Env = append(os.Environ(), "CGO_ENABLED=1")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build gt-index: %v\n%s", err, out)
	}
	dbPath := filepath.Join(tmp, "graph.db")
	index := exec.Command(bin, "-root", repo, "-output", dbPath, "-workers", "2")
	if out, err := index.CombinedOutput(); err != nil {
		t.Fatalf("full index: %v\n%s", err, out)
	}
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var complete, failures string
	if err := db.QueryRow("SELECT value FROM project_meta WHERE key='resolution_complete'").Scan(&complete); err != nil {
		t.Fatalf("resolution_complete metadata: %v", err)
	}
	if complete != "1" {
		t.Fatalf("resolution_complete = %q, want 1", complete)
	}
	if err := db.QueryRow("SELECT value FROM project_meta WHERE key='parse_failures'").Scan(&failures); err != nil {
		t.Fatalf("parse_failures metadata: %v", err)
	}
	if failures != "0" {
		t.Fatalf("parse_failures = %q, want 0", failures)
	}
	var calls, candidates, symbols int
	if err := db.QueryRow("SELECT count(*) FROM resolution_callsites").Scan(&calls); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT count(*) FROM resolution_candidates").Scan(&candidates); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT count(*) FROM resolution_symbols").Scan(&symbols); err != nil {
		t.Fatal(err)
	}
	if calls == 0 || candidates == 0 || symbols == 0 {
		t.Fatalf("producer sidecar is empty: callsites=%d candidates=%d symbols=%d", calls, candidates, symbols)
	}
	var orphan int
	if err := db.QueryRow(`SELECT count(*) FROM resolution_candidates c
		LEFT JOIN resolution_symbols s ON s.stable_id=c.target_stable_id
		WHERE s.stable_id IS NULL`).Scan(&orphan); err != nil {
		t.Fatal(err)
	}
	if orphan != 0 {
		t.Fatalf("candidate rows with no producer symbol identity: %d", orphan)
	}
	var mismatch int
	if err := db.QueryRow(`SELECT count(*) FROM resolution_callsites c
		WHERE c.candidate_count != (SELECT count(*) FROM resolution_candidates k WHERE k.callsite_id=c.callsite_id)`).Scan(&mismatch); err != nil {
		t.Fatal(err)
	}
	if mismatch != 0 {
		t.Fatalf("callsite candidate counts disagree with retained rows: %d", mismatch)
	}
	var dynamicCount, dynamicCandidates, dynamicSelected int
	if err := db.QueryRow(`SELECT count(*),coalesce(sum(candidate_count),0),count(selected_target_stable_id)
		FROM resolution_callsites WHERE dispatch_state='dynamic'`).Scan(&dynamicCount, &dynamicCandidates, &dynamicSelected); err != nil {
		t.Fatal(err)
	}
	if dynamicCount == 0 || dynamicCandidates != 0 || dynamicSelected != 0 {
		t.Fatalf("production dynamic policy violated: callsites=%d candidates=%d selected=%d", dynamicCount, dynamicCandidates, dynamicSelected)
	}
	var declaredScope, importChain, verification string
	if err := db.QueryRow(`SELECT declared_scope,import_chain,verification_status
		FROM resolution_candidates WHERE import_chain != '[]' ORDER BY callsite_id,ordinal LIMIT 1`).Scan(&declaredScope, &importChain, &verification); err != nil {
		t.Fatal(err)
	}
	if declaredScope == "" || importChain == "" || importChain == "[]" || verification != "source_supported" {
		t.Fatalf("candidate evidence is not source-bound: scope=%q import_chain=%q verification=%q", declaredScope, importChain, verification)
	}
	var graphComplete, graphRevision, graphResolutionSchema string
	if err := db.QueryRow("SELECT value FROM project_meta WHERE key='graph_resolution_complete'").Scan(&graphComplete); err != nil {
		t.Fatalf("graph_resolution_complete metadata: %v", err)
	}
	if graphComplete != "1" {
		t.Fatalf("graph_resolution_complete = %q, want 1", graphComplete)
	}
	if err := db.QueryRow("SELECT value FROM project_meta WHERE key='graph_resolution_revision'").Scan(&graphRevision); err != nil {
		t.Fatalf("graph_resolution_revision metadata: %v", err)
	}
	if graphRevision == "" {
		t.Fatal("graph_resolution_revision is empty")
	}
	if err := db.QueryRow("SELECT value FROM project_meta WHERE key='graph_resolution_schema_version'").Scan(&graphResolutionSchema); err != nil {
		t.Fatalf("graph_resolution_schema_version metadata: %v", err)
	}
	if graphResolutionSchema != "2" {
		t.Fatalf("graph_resolution_schema_version = %q, want 2", graphResolutionSchema)
	}
	var callsitesV2, candidatesV2, derivationsV2, completenessV2, policiesV2 int
	if err := db.QueryRow(`SELECT
		(SELECT count(*) FROM nodes WHERE node_type='callsite' AND schema_version=2),
		(SELECT count(*) FROM edges WHERE type='CANDIDATE_TARGET' AND schema_version=2 AND confidence IS NULL),
		(SELECT count(*) FROM nodes WHERE node_type='derivation_fact' AND schema_version=2),
		(SELECT count(*) FROM nodes WHERE node_type='completeness_fact' AND schema_version=2),
		(SELECT count(*) FROM nodes WHERE node_type='query_policy_version' AND schema_version=2)`).Scan(&callsitesV2, &candidatesV2, &derivationsV2, &completenessV2, &policiesV2); err != nil {
		t.Fatalf("canonical v2 graph counts: %v", err)
	}
	if callsitesV2 == 0 || candidatesV2 == 0 || derivationsV2 != candidatesV2 || completenessV2 == 0 || policiesV2 != 2 {
		t.Fatalf("canonical v2 graph incomplete: callsites=%d candidates=%d derivations=%d completeness=%d policies=%d", callsitesV2, candidatesV2, derivationsV2, completenessV2, policiesV2)
	}
	var conservationMismatch int
	if err := db.QueryRow(`SELECT count(*) FROM nodes c WHERE c.node_type='callsite'
		AND c.candidate_count_v2 != (SELECT count(*) FROM edges e WHERE e.type='CANDIDATE_TARGET' AND e.callsite_stable_id=c.stable_id AND e.viability='viable')`).Scan(&conservationMismatch); err != nil {
		t.Fatalf("canonical v2 candidate conservation: %v", err)
	}
	if conservationMismatch != 0 {
		t.Fatalf("canonical v2 candidate conservation mismatches=%d", conservationMismatch)
	}
	for _, key := range []string{
		"graph_completion_schema", "graph_completion_receipt", "graph_completion_receipt_sha256",
		"graph_producer_build_id", "graph_producer_source_fingerprint", "graph_producer_executable_sha256",
		"graph_producer_git_commit", "graph_producer_build_time_utc", "graph_producer_go_toolchain", "graph_producer_build_tags",
	} {
		var value string
		if err := db.QueryRow("SELECT value FROM project_meta WHERE key=?", key).Scan(&value); err != nil || value == "" || value == "unknown" {
			t.Fatalf("graph/build binding %s missing or incomplete: value=%q err=%v", key, value, err)
		}
	}
	var attached int
	if err := db.QueryRow(`SELECT count(*) FROM edges WHERE type='HAS_CALLSITE'`).Scan(&attached); err != nil {
		t.Fatal(err)
	}
	if attached == 0 {
		t.Fatal("primary graph has no attached callsite relationships")
	}
	var duplicateCalls int
	if err := db.QueryRow(`SELECT count(*) FROM (
		SELECT source_id,target_id,type FROM edges WHERE type='CALLS'
		GROUP BY source_id,target_id,type HAVING count(*) > 1)`).Scan(&duplicateCalls); err != nil {
		t.Fatal(err)
	}
	if duplicateCalls != 0 {
		t.Fatalf("selected callsites published duplicate generic CALLS endpoints: %d", duplicateCalls)
	}
	var selectedBindings int
	if err := db.QueryRow(`SELECT count(*) FROM edges WHERE type='SELECTED_TARGET' AND schema_version=2`).Scan(&selectedBindings); err != nil {
		t.Fatal(err)
	}
	if selectedBindings == 0 {
		t.Fatal("canonical v2 selection is not bound by SELECTED_TARGET")
	}
	var coverageJSON string
	if err := db.QueryRow(`SELECT hc.pass_coverage FROM nodes c
		JOIN edges hc ON hc.target_id=c.id AND hc.type='HAS_CALLSITE'
		WHERE c.node_type='callsite' AND c.name='local_target' LIMIT 1`).Scan(&coverageJSON); err != nil {
		t.Fatalf("direct-call pass coverage: %v", err)
	}
	var coverage []store.ResolutionPassCoverage
	if err := json.Unmarshal([]byte(coverageJSON), &coverage); err != nil {
		t.Fatalf("decode direct-call pass coverage: %v", err)
	}
	statuses := make(map[string]store.ResolutionPassCoverage, len(coverage))
	for _, pass := range coverage {
		statuses[pass.PassKind] = pass
	}
	if got := statuses["lexical_binding"]; got.Status != "completed_match" {
		t.Fatalf("direct binding execution status=%+v, want completed_match", got)
	}
	for _, passKind := range []string{"import_binding", "declared_type", "implementation_set", "return_type", "global_name"} {
		got := statuses[passKind]
		if got.Status != "not_run" || got.Reason == "" {
			t.Fatalf("unexecuted pass %s fabricated completion: %+v", passKind, got)
		}
	}
	if err := db.QueryRow(`SELECT hc.pass_coverage FROM nodes c
		JOIN edges hc ON hc.target_id=c.id AND hc.type='HAS_CALLSITE'
		WHERE c.node_type='callsite' AND c.name='target' LIMIT 1`).Scan(&coverageJSON); err != nil {
		t.Fatalf("import-call pass coverage: %v", err)
	}
	coverage = nil
	if err := json.Unmarshal([]byte(coverageJSON), &coverage); err != nil {
		t.Fatalf("decode import-call pass coverage: %v", err)
	}
	statuses = make(map[string]store.ResolutionPassCoverage, len(coverage))
	for _, pass := range coverage {
		statuses[pass.PassKind] = pass
	}
	if got := statuses["lexical_binding"]; got.Status != "completed_no_match" || got.Reason != "" {
		t.Fatalf("attempted lexical miss before import winner was not witnessed: %+v", got)
	}
	if got := statuses["import_binding"]; got.Status != "completed_match" || got.Reason != "" {
		t.Fatalf("import winner execution was not witnessed: %+v", got)
	}
	for _, passKind := range []string{"declared_type", "implementation_set", "return_type", "global_name"} {
		got := statuses[passKind]
		if got.Status != "not_run" || got.Reason == "" {
			t.Fatalf("post-import pass %s fabricated execution: %+v", passKind, got)
		}
	}
	graph, err := store.Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer graph.Close()
	evidence, err := graph.QueryAttachedCandidates("target")
	if err != nil {
		t.Fatalf("production graph query: %v", err)
	}
	if len(evidence) == 0 || evidence[0].Revision != graphRevision {
		t.Fatalf("production graph query returned no revision-bound evidence: %+v", evidence)
	}
	dynamicEvidence, err := graph.QueryAttachedCandidates("handlers[name]")
	if err != nil {
		t.Fatalf("production zero-candidate query: %v", err)
	}
	if len(dynamicEvidence) != 1 || dynamicEvidence[0].HasCandidate || dynamicEvidence[0].DispatchState != "unsupported" || dynamicEvidence[0].TargetAuthority != "unsupported" || dynamicEvidence[0].AbstentionReason != "unsupported_dispatch" {
		t.Fatalf("production query hid or misrepresented dynamic abstention: %+v", dynamicEvidence)
	}
}

func TestRealCLIUsesDeclaredGoInterfaceBoundaryForCHA(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	src := `package sample

type Runner interface { Run(int) error }
type Unrelated interface { Other() }

type ImplA struct{}
func (ImplA) Run(int) error { return nil }

type ImplUnrelated struct{}
func (ImplUnrelated) Run(int) string { return "wrong" }

func Invoke(runner Runner) {
	runner.Run(1)
}
`
	if err := os.WriteFile(filepath.Join(repo, "sample.go"), []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}
	bin := filepath.Join(tmp, "gt-index")
	if runtime.GOOS == "windows" {
		bin += ".exe"
	}
	build := exec.Command("go", "build", "-tags", "sqlite_fts5", "-ldflags", testBuildLDFlags, "-o", bin, ".")
	build.Env = append(os.Environ(), "CGO_ENABLED=1")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build gt-index: %v\n%s", err, out)
	}
	dbPath := filepath.Join(tmp, "graph.db")
	index := exec.Command(bin, "-root", repo, "-output", dbPath, "-workers", "2")
	index.Env = append(os.Environ(), "GT_HIERARCHY_CLOSED=1")
	if out, err := index.CombinedOutput(); err != nil {
		t.Fatalf("full Go hierarchy index: %v\n%s", err, out)
	}
	graph, err := store.Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer graph.Close()
	evidence, err := graph.QueryAttachedCandidates("Run")
	if err != nil {
		t.Fatalf("Go hierarchy query: %v", err)
	}
	if len(evidence) != 1 {
		t.Fatalf("Go interface callsites=%d, want 1: %+v", len(evidence), evidence)
	}
	if evidence[0].CandidateCount != 1 || len(evidence[0].PassCoverage) == 0 {
		t.Fatalf("declared Runner boundary was not retained: %+v", evidence[0])
	}
	var cha store.ResolutionPassCoverage
	for _, pass := range evidence[0].PassCoverage {
		if pass.PassKind == "cha" {
			cha = pass
		}
	}
	if cha.Status != "closed" || len(cha.CandidateStableIDs) != 1 || cha.CandidateStableIDs[0] != evidence[0].TargetStableID {
		t.Fatalf("normal query lost actual CHA candidate identity: candidate=%+v cha=%+v", evidence[0], cha)
	}
	raw, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer raw.Close()
	var qualified string
	if err := raw.QueryRow(`SELECT qualified_name FROM resolution_symbols WHERE stable_id=?`, evidence[0].TargetStableID).Scan(&qualified); err != nil {
		t.Fatalf("lookup retained CHA target: %v", err)
	}
	if qualified != "ImplA.Run" {
		t.Fatalf("declared Runner selected wrong concrete method %q", qualified)
	}
}

func TestRealCLIRecordsRecoverableParserFailures(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, "broken.js"), []byte("function run() { target(); }\n@\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, "clean.js"), []byte("function target() { return 1; }\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	bin := filepath.Join(tmp, "gt-index")
	if runtime.GOOS == "windows" {
		bin += ".exe"
	}
	build := exec.Command("go", "build", "-tags", "sqlite_fts5", "-ldflags", testBuildLDFlags, "-o", bin, ".")
	build.Env = append(os.Environ(), "CGO_ENABLED=1")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build gt-index: %v\n%s", err, out)
	}
	dbPath := filepath.Join(tmp, "graph.db")
	if out, err := exec.Command(bin, "-root", repo, "-output", dbPath).CombinedOutput(); err != nil {
		t.Fatalf("index malformed fixture: %v\n%s", err, out)
	}
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var failures string
	if err := db.QueryRow("SELECT value FROM project_meta WHERE key='parse_failures'").Scan(&failures); err != nil {
		t.Fatal(err)
	}
	if failures != "1" {
		t.Fatalf("parse_failures=%q, want 1", failures)
	}
	var incomplete int
	if err := db.QueryRow("SELECT count(*) FROM resolution_callsites WHERE dispatch_state='parser_incomplete'").Scan(&incomplete); err != nil {
		t.Fatal(err)
	}
	if incomplete == 0 {
		t.Fatal("partial AST callsite was not retained as parser_incomplete")
	}
}
