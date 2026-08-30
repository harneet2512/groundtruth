package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"
)

const testBuildLDFlags = "-X main.commitSHA=test-commit -X main.buildTimeUTC=2026-08-29T00:00:00Z -X main.sourceFingerprint=test-source -X main.compiledBuildTags=sqlite_fts5 -X main.goToolchain=test-toolchain"

func TestBuildInfoReportsSourceBoundExecutableIdentity(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	tmp := t.TempDir()
	bin := filepath.Join(tmp, "gt-index")
	if runtime.GOOS == "windows" {
		bin += ".exe"
	}
	ldflags := "-X main.commitSHA=0123456789abcdef -X main.buildTimeUTC=2026-08-29T00:00:00Z -X main.sourceFingerprint=source-fixture -X main.compiledBuildTags=sqlite_fts5 -X main.goToolchain=test-toolchain"
	build := exec.Command("go", "build", "-tags", "sqlite_fts5", "-ldflags", ldflags, "-o", bin, ".")
	build.Env = append(os.Environ(), "CGO_ENABLED=1")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build gt-index: %v\n%s", err, out)
	}
	out, err := exec.Command(bin, "-build-info").Output()
	if err != nil {
		t.Fatal(err)
	}
	var info buildIdentity
	if err := json.Unmarshal(out, &info); err != nil {
		t.Fatalf("decode build identity: %v: %s", err, out)
	}
	if info.Schema != buildInfoSchema || !info.Complete || info.SourceFingerprint != "source-fixture" || info.BuildID == "" {
		t.Fatalf("incomplete source-bound identity: %+v", info)
	}
	bytes, err := os.ReadFile(bin)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(bytes)
	if info.ExecutableSHA256 != hex.EncodeToString(sum[:]) {
		t.Fatalf("executable hash mismatch: got %s", info.ExecutableSHA256)
	}
}

func TestBuildIdentityChangesWithCommitTimeSourceHeaderOrToolchainFingerprint(t *testing.T) {
	originalCommit, originalTime, originalSource, originalTags, originalToolchain := commitSHA, buildTimeUTC, sourceFingerprint, compiledBuildTags, goToolchain
	t.Cleanup(func() {
		commitSHA, buildTimeUTC, sourceFingerprint, compiledBuildTags, goToolchain = originalCommit, originalTime, originalSource, originalTags, originalToolchain
	})
	commitSHA, buildTimeUTC, compiledBuildTags = "commit", "2026-08-29T00:00:00Z", "sqlite_fts5"
	sourceFingerprint, goToolchain = "source-and-headers-v1", "go1"
	first, err := currentBuildIdentity()
	if err != nil {
		t.Fatal(err)
	}
	sourceFingerprint = "source-and-headers-v2"
	second, err := currentBuildIdentity()
	if err != nil {
		t.Fatal(err)
	}
	goToolchain = "go2"
	third, err := currentBuildIdentity()
	if err != nil {
		t.Fatal(err)
	}
	commitSHA = "commit-2"
	fourth, err := currentBuildIdentity()
	if err != nil {
		t.Fatal(err)
	}
	buildTimeUTC = "2026-08-29T00:00:01Z"
	fifth, err := currentBuildIdentity()
	if err != nil {
		t.Fatal(err)
	}
	if first.BuildID == second.BuildID || second.BuildID == third.BuildID || third.BuildID == fourth.BuildID || fourth.BuildID == fifth.BuildID {
		t.Fatalf("build identity did not bind commit/time/source/header/toolchain changes: %q %q %q %q %q", first.BuildID, second.BuildID, third.BuildID, fourth.BuildID, fifth.BuildID)
	}
}
