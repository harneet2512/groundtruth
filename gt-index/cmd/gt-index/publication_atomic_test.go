package main

import (
	"bytes"
	"database/sql"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

func TestFailedFullBuildPreservesPreviousAuthoritativeArtifact(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
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

	output := filepath.Join(tmp, "graph.db")
	prior := []byte("prior-complete-authoritative-graph")
	if err := os.WriteFile(output, prior, 0o600); err != nil {
		t.Fatal(err)
	}
	missingRoot := filepath.Join(tmp, "does-not-exist")
	cmd := exec.Command(bin, "-root", missingRoot, "-output", output)
	cmd.Env = append(os.Environ(), "GT_REQUIRE_PARSE_RATE=not-a-number")
	if out, err := cmd.CombinedOutput(); err == nil {
		t.Fatalf("invalid full build unexpectedly succeeded: %s", out)
	}
	got, err := os.ReadFile(output)
	if err != nil {
		t.Fatalf("read prior graph after failed build: %v", err)
	}
	if !bytes.Equal(got, prior) {
		t.Fatalf("failed full build replaced the prior authoritative artifact: got %q", got)
	}
	leftovers, err := filepath.Glob(filepath.Join(tmp, ".graph.db.building-*"))
	if err != nil {
		t.Fatal(err)
	}
	if len(leftovers) != 0 {
		t.Fatalf("failed build leaked staged artifacts: %v", leftovers)
	}
}

func TestPublishStagedOutputSwapsCompleteArtifactForConcurrentReaders(t *testing.T) {
	tmp := t.TempDir()
	target := filepath.Join(tmp, "graph.db")
	oldBytes := []byte("old-complete-graph")
	newBytes := []byte("new-complete-graph")
	if err := os.WriteFile(target, oldBytes, 0o600); err != nil {
		t.Fatal(err)
	}
	reader, err := os.Open(target)
	if err != nil {
		t.Fatal(err)
	}
	staged, err := createStagedOutput(target)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(staged, newBytes, 0o600); err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS == "windows" {
		if err := publishStagedOutput(staged, target); err == nil {
			t.Fatal("Windows publication unexpectedly replaced a graph held by an active reader")
		}
		stillOld, err := os.ReadFile(target)
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(stillOld, oldBytes) {
			t.Fatalf("failed publication changed the authoritative graph: got %q", stillOld)
		}
		if err := reader.Close(); err != nil {
			t.Fatal(err)
		}
		if err := publishStagedOutput(staged, target); err != nil {
			t.Fatal(err)
		}
	} else {
		defer reader.Close()
		if err := publishStagedOutput(staged, target); err != nil {
			t.Fatal(err)
		}
	}
	oldView, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(oldView, newBytes) {
		t.Fatalf("path did not expose the complete replacement: got %q", oldView)
	}
	if runtime.GOOS != "windows" {
		readerView := make([]byte, len(oldBytes))
		if _, err := reader.ReadAt(readerView, 0); err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(readerView, oldBytes) {
			t.Fatalf("existing reader observed a mixed or replaced snapshot: got %q", readerView)
		}
	}
	if _, err := os.Stat(staged); !os.IsNotExist(err) {
		t.Fatalf("staged artifact survived publication: %v", err)
	}
}

func TestPublishStagedSQLiteNeverExposesMixedDatabase(t *testing.T) {
	tmp := t.TempDir()
	target := filepath.Join(tmp, "graph.db")
	writeSQLiteVersion := func(path string, version int) {
		db, err := sql.Open("sqlite3", path)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := db.Exec("PRAGMA journal_mode=WAL; CREATE TABLE marker(version INTEGER); INSERT INTO marker VALUES (?)", version); err != nil {
			_ = db.Close()
			t.Fatal(err)
		}
		if _, err := db.Exec("PRAGMA wal_checkpoint(TRUNCATE)"); err != nil {
			_ = db.Close()
			t.Fatal(err)
		}
		if err := db.Close(); err != nil {
			t.Fatal(err)
		}
	}
	writeSQLiteVersion(target, 1)
	reader, err := sql.Open("sqlite3", target)
	if err != nil {
		t.Fatal(err)
	}
	tx, err := reader.Begin()
	if err != nil {
		t.Fatal(err)
	}
	var oldVersion int
	if err := tx.QueryRow("SELECT version FROM marker").Scan(&oldVersion); err != nil {
		t.Fatal(err)
	}
	staged, err := createStagedOutput(target)
	if err != nil {
		t.Fatal(err)
	}
	// SQLite needs to initialize an empty path, not an existing zero-byte file.
	if err := os.Remove(staged); err != nil {
		t.Fatal(err)
	}
	writeSQLiteVersion(staged, 2)
	publishErr := publishStagedOutput(staged, target)
	if runtime.GOOS == "windows" && publishErr != nil {
		// Windows may refuse replacement while a reader owns the file. This is
		// the required fail-closed behavior; retry only after the reader exits.
		if err := tx.Rollback(); err != nil {
			t.Fatal(err)
		}
		if err := reader.Close(); err != nil {
			t.Fatal(err)
		}
		if err := publishStagedOutput(staged, target); err != nil {
			t.Fatal(err)
		}
	} else {
		if publishErr != nil {
			t.Fatal(publishErr)
		}
		if oldVersion != 1 {
			t.Fatalf("existing reader lost prior snapshot: %d", oldVersion)
		}
		_ = tx.Rollback()
		_ = reader.Close()
	}
	newReader, err := sql.Open("sqlite3", target)
	if err != nil {
		t.Fatal(err)
	}
	defer newReader.Close()
	var newVersion int
	if err := newReader.QueryRow("SELECT version FROM marker").Scan(&newVersion); err != nil {
		t.Fatal(err)
	}
	if newVersion != 2 {
		t.Fatalf("new reader saw version %d, want complete version 2", newVersion)
	}
}
