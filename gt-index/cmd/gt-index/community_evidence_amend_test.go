package main

import (
	"database/sql"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// TestBatchAmendCarriedCommunityEvidenceCitesLiveEdges: when a batch amend
// carries the parent's community partition (history window and certified
// call digest unchanged), every evidence entry that cites an edges.id must
// name a CERTIFIED CALLS edge of the PUBLISHED graph. The amend re-inserts
// edges, so the parent's ids named rows the amended graph no longer has.
func TestBatchAmendCarriedCommunityEvidenceCitesLiveEdges(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	bin := buildDerivedIndexer(t)
	root := t.TempDir()
	repo := filepath.Join(root, "repo")
	writeDerivedFixtureRepo(t, repo)
	env := append(os.Environ(), "GT_PARSE_CACHE_ROOT="+filepath.Join(root, "cache"))
	parent := filepath.Join(root, "parent.db")
	cmd := exec.Command(bin, "-root", repo, "-output", parent)
	cmd.Env = env
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("parent: %v\n%s", err, out)
	}
	// A body edit that keeps every cross-file call: the digest matches, so
	// the partition is carried.
	modPath := filepath.Join(repo, "mod.py")
	src, err := os.ReadFile(modPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(modPath, []byte(strings.Replace(string(src), "value + 1\n", "value + 2\n", 1)), 0o600); err != nil {
		t.Fatal(err)
	}
	candidate := filepath.Join(root, "candidate.db")
	amend := exec.Command(bin, "-root", repo, "-output", candidate, "-amend-parent", parent)
	amend.Env = env
	if out, err := amend.CombinedOutput(); err != nil {
		t.Fatalf("amend: %v\n%s", err, out)
	}
	db, err := sql.Open("sqlite3", candidate)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if reused, _ := derivedMetaValue(db, "derived_coupling_reused"); reused != "cochange,community" {
		t.Fatalf("fixture must exercise the carried-partition path; derived_coupling_reused=%q", reused)
	}
	rows, err := db.Query(`SELECT id, evidence_edge_ids FROM communities`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	cited := 0
	for rows.Next() {
		var id, raw string
		if err := rows.Scan(&id, &raw); err != nil {
			t.Fatal(err)
		}
		var evidence []string
		if err := json.Unmarshal([]byte(raw), &evidence); err != nil {
			t.Fatal(err)
		}
		for _, e := range evidence {
			edgeID, err := strconv.ParseInt(e, 10, 64)
			if err != nil {
				continue // cochange:<a>|<b> marker
			}
			cited++
			var typ, tier string
			if err := db.QueryRow(`SELECT type, trust_tier FROM edges WHERE id=?`, edgeID).Scan(&typ, &tier); err != nil {
				t.Errorf("community %s cites edge %d, which the published graph does not have: %v", id, edgeID, err)
				continue
			}
			if typ != "CALLS" || tier != "CERTIFIED" {
				t.Errorf("community %s cites edge %d as call evidence but it is %s/%s", id, edgeID, typ, tier)
			}
		}
	}
	if cited == 0 {
		t.Fatal("fixture communities cite no call edges; the test would be vacuous")
	}
}
