package main

import (
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
)

func TestBuildDeclaresBoundedBatchParserReuse(t *testing.T) {
	for _, capability := range declaredBuildIdentity().Capabilities {
		if capability == "batch_parser_node_reuse_v1" {
			return
		}
	}
	t.Fatal("installed caller cannot select the proven batch parser-node reuse API")
}

func TestBatchAmendRetainsUnchangedStructureAndParent(t *testing.T) {
	bin := buildDerivedIndexer(t)
	root := t.TempDir()
	repo := filepath.Join(root, "repo")
	writeDerivedFixtureRepo(t, repo)
	callerPath := filepath.Join(repo, "caller.py")
	callerSource, err := os.ReadFile(callerPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(callerPath, append(callerSource, []byte("\nclass Stable:\n    def method(self):\n        return 1\n")...), 0600); err != nil {
		t.Fatal(err)
	}
	parent := filepath.Join(root, "parent.db")
	cmd := exec.Command(bin, "-root", repo, "-output", parent)
	cmd.Env = append(os.Environ(), "GT_PARSE_CACHE_ROOT="+filepath.Join(root, "cache"))
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("base: %v\n%s", err, out)
	}
	before, err := os.ReadFile(parent)
	if err != nil {
		t.Fatal(err)
	}
	oldCaller := batchNodeID(t, parent, "caller.py", "run")
	propertyQuery := `SELECT p.id,p.kind,p.value,p.line FROM properties p JOIN nodes n ON n.id=p.node_id WHERE n.file_path='caller.py' AND p.kind='param'`
	oldProperties := batchQueryRows(t, parent, propertyQuery)
	edgeQuery := `SELECT e.id,e.source_id,e.target_id,e.type FROM edges e WHERE e.source_file='caller.py' AND e.type='CONTAINS'`
	oldEdges := batchQueryRows(t, parent, edgeQuery)
	if len(oldEdges) == 0 {
		t.Fatal("fixture has no containment edges")
	}
	if len(oldProperties) == 0 {
		t.Fatal("fixture has no unchanged parser properties")
	}
	oldLeaf := batchNodeID(t, parent, "mod.py", "leaf")
	path := filepath.Join(repo, "mod.py")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(strings.Replace(string(data), "value + 1", "value + 2", 1)), 0600); err != nil {
		t.Fatal(err)
	}
	candidate := filepath.Join(root, "candidate.db")
	amend := exec.Command(bin, "-root", repo, "-output", candidate, "-amend-parent", parent)
	amend.Env = cmd.Env
	out, err := amend.Output()
	if err != nil {
		t.Fatalf("amend: %v\n%s", err, out)
	}
	var summary struct {
		Mode               string `json:"build_mode"`
		Retained           int    `json:"parser_nodes_retained"`
		Inserted           int    `json:"parser_nodes_inserted"`
		CacheHits          int    `json:"parse_cache_hits"`
		CacheMisses        int    `json:"parse_cache_misses"`
		ResolverPasses     int    `json:"resolver_passes"`
		PropertiesRetained int    `json:"parser_properties_retained"`
		EdgesRetained      int    `json:"parser_edges_retained"`
	}
	if err := json.Unmarshal(out, &summary); err != nil {
		t.Fatalf("invalid producer summary: %v: %s", err, out)
	}
	if summary.Mode != "batch" || summary.Retained < 1 || summary.Inserted < 1 || summary.CacheHits != 2 || summary.CacheMisses != 1 || summary.ResolverPasses != 1 {
		t.Fatalf("missing or inaccurate batch work counters: %+v", summary)
	}
	if summary.PropertiesRetained < len(oldProperties) || summary.EdgesRetained < len(oldEdges) {
		t.Fatalf("missing structural work counters: %+v", summary)
	}
	if got := batchNodeID(t, candidate, "caller.py", "run"); got != oldCaller {
		t.Fatalf("unchanged node rebuilt: %d -> %d", oldCaller, got)
	}
	if got := batchQueryRows(t, candidate, propertyQuery); !reflect.DeepEqual(got, oldProperties) {
		t.Fatalf("unchanged parser properties rebuilt: %v -> %v", oldProperties, got)
	}
	if got := batchQueryRows(t, candidate, edgeQuery); !reflect.DeepEqual(got, oldEdges) {
		t.Fatalf("unchanged containment edges rebuilt: %v -> %v", oldEdges, got)
	}
	if got := batchNodeID(t, candidate, "mod.py", "leaf"); got == oldLeaf {
		t.Fatal("changed structural row was not replaced")
	}
	after, err := os.ReadFile(parent)
	if err != nil {
		t.Fatal(err)
	}
	if sha256.Sum256(before) != sha256.Sum256(after) {
		t.Fatal("certified parent was mutated")
	}
}

func TestBatchAmendMatchesFreshCoreAndResolution(t *testing.T) {
	bin := buildDerivedIndexer(t)
	for _, change := range []string{"unchanged", "edit", "add", "delete", "rename", "import", "inheritance", "ambiguity", "new_target"} {
		t.Run(change, func(t *testing.T) {
			root := t.TempDir()
			repo := filepath.Join(root, "repo")
			writeDerivedFixtureRepo(t, repo)
			build := func(name, parent string) string {
				dest := filepath.Join(root, name+".db")
				args := []string{"-root", repo, "-output", dest}
				if parent != "" {
					args = append(args, "-amend-parent", parent)
				}
				cmd := exec.Command(bin, args...)
				cmd.Env = append(os.Environ(), "GT_PARSE_CACHE_ROOT="+filepath.Join(root, "cache"), "GT_REQUIRE_FTS5=1")
				if out, err := cmd.CombinedOutput(); err != nil {
					t.Fatalf("%s: %v\n%s", name, err, out)
				}
				return dest
			}
			parent := build("parent", "")
			write := func(file, text string) {
				if err := os.WriteFile(filepath.Join(repo, file), []byte(text), 0600); err != nil {
					t.Fatal(err)
				}
			}
			switch change {
			case "edit":
				write("mod.py", "def leaf(value):\n    return value + 2\ndef entry(value):\n    return leaf(value)\n")
			case "add":
				write("new.py", "from mod import leaf\ndef extra(value):\n    return leaf(value)\n")
			case "delete":
				if err := os.Remove(filepath.Join(repo, "mod.py")); err != nil {
					t.Fatal(err)
				}
			case "rename":
				if err := os.Rename(filepath.Join(repo, "mod.py"), filepath.Join(repo, "renamed.py")); err != nil {
					t.Fatal(err)
				}
			case "import":
				write("new.py", "def entry(value):\n    return value * 2\n")
				write("caller.py", "from new import entry\ndef run(value):\n    return entry(value)\n")
			case "inheritance":
				write("mod.py", "class Base:\n    def leaf(self, value):\n        return value + 1\n")
				write("caller.py", "from mod import Base\nclass Child(Base):\n    def run(self, value):\n        return self.leaf(value)\n")
			case "ambiguity":
				write("new.py", "def leaf(value):\n    return value * 3\n")
				write("caller.py", "def run(value):\n    return leaf(value)\n")
			case "new_target":
				write("new.py", "def newly_added(value):\n    return value * 2\n")
				write("caller.py", "from new import newly_added\ndef run(value):\n    return newly_added(value)\n")
			}
			amended, fresh := build("amended", parent), build("fresh", "")
			queries := map[string]string{
				"structure":  `SELECT n.label,n.name,n.qualified_name,n.file_path,n.start_line,n.end_line,n.signature,n.return_type,n.is_exported,n.is_test,n.language,n.file_hash,n.byte_start,n.byte_end,p.qualified_name FROM nodes n JOIN parser_node_inventory i ON i.node_id=n.id LEFT JOIN nodes p ON p.id=n.parent_id`,
				"edges":      `SELECT s.file_path,s.qualified_name,s.label,t.file_path,t.qualified_name,t.label,e.type,e.source_line,e.source_file,e.resolution_method,e.confidence,e.trust_tier,e.candidate_count,e.evidence_type,e.verification_status FROM edges e JOIN nodes s ON s.id=e.source_id JOIN nodes t ON t.id=e.target_id WHERE e.type NOT IN ('HAS_CALLSITE','CANDIDATE_TARGET') AND COALESCE(s.node_type,'')='' AND COALESCE(t.node_type,'')=''`,
				"properties": `SELECT n.file_path,n.qualified_name,p.kind,p.value,p.line,p.confidence FROM properties p JOIN nodes n ON n.id=p.node_id`,
				"assertions": `SELECT n.file_path,n.qualified_name,t.file_path,t.qualified_name,a.resolution_score,a.kind,a.expression,a.expected,a.line FROM assertions a JOIN nodes n ON n.id=a.test_node_id LEFT JOIN nodes t ON t.id=a.target_node_id`,
				"callsites":  `SELECT c.source_file,c.source_line,c.callee,c.language,c.dispatch_state,c.candidate_count,c.mechanism,c.verification_status,s.path,s.qualified_name FROM resolution_callsites c LEFT JOIN resolution_symbols s ON s.stable_id=c.selected_target_stable_id`,
				"candidates": `SELECT c.source_file,c.source_line,c.callee,t.file_path,t.qualified_name,r.ordinal,r.mechanism,r.declared_scope,r.receiver_type,r.receiver_origin,r.receiver_shape,r.receiver_chain,r.import_chain,r.dynamic_dispatch,r.export_status,r.parser_complete,r.verification_status,r.selected FROM resolution_candidates r JOIN resolution_callsites c ON c.callsite_id=r.callsite_id JOIN nodes t ON t.id=r.target_id`,
				"closure":    `SELECT s.file_path,s.qualified_name,t.file_path,t.qualified_name,c.depth,c.min_confidence FROM closure c JOIN nodes s ON s.id=c.source_id JOIN nodes t ON t.id=c.target_id`,
				"cochanges":  `SELECT * FROM cochanges`,
			}
			for name, query := range queries {
				a, b := batchQueryRows(t, amended, query), batchQueryRows(t, fresh, query)
				if !reflect.DeepEqual(a, b) {
					t.Errorf("%s changed\namended: %v\nfresh: %v", name, a, b)
				}
			}
		})
	}
}

func batchQueryRows(t *testing.T, path, query string) []string {
	t.Helper()
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	rows, err := db.Query(query)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	cols, err := rows.Columns()
	if err != nil {
		t.Fatal(err)
	}
	var result []string
	for rows.Next() {
		values := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range values {
			ptrs[i] = &values[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			t.Fatal(err)
		}
		result = append(result, fmt.Sprint(values))
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	sort.Strings(result)
	return result
}

func batchNodeID(t *testing.T, path, file, name string) int64 {
	t.Helper()
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var id int64
	if err := db.QueryRow("SELECT id FROM nodes WHERE file_path=? AND name=? AND label='Function'", file, name).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return id
}
