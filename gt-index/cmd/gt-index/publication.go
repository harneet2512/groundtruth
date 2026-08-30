package main

import (
	"fmt"
	"log"
	"os"
	"path/filepath"

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

func createStagedOutput(target string) (string, error) {
	dir := filepath.Dir(target)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", fmt.Errorf("create output directory: %w", err)
	}
	f, err := os.CreateTemp(dir, "."+filepath.Base(target)+".building-*")
	if err != nil {
		return "", err
	}
	name := f.Name()
	if err := f.Close(); err != nil {
		_ = os.Remove(name)
		return "", err
	}
	if info, statErr := os.Stat(target); statErr == nil {
		if err := os.Chmod(name, info.Mode().Perm()); err != nil {
			_ = os.Remove(name)
			return "", fmt.Errorf("preserve output permissions: %w", err)
		}
	}
	return name, nil
}

func cleanupStagedOutput(staged string) {
	for _, path := range []string{staged, staged + "-wal", staged + "-shm"} {
		_ = os.Remove(path)
	}
}

func abortStagedBuild(db *store.DB, staged, format string, args ...any) {
	if db != nil {
		_ = db.Close()
	}
	cleanupStagedOutput(staged)
	log.Fatalf(format, args...)
}

func publishStagedOutput(staged, target string) error {
	if staged == "" || target == "" {
		return fmt.Errorf("staged and target paths are required")
	}
	stagedAbs, err := filepath.Abs(staged)
	if err != nil {
		return err
	}
	targetAbs, err := filepath.Abs(target)
	if err != nil {
		return err
	}
	if filepath.Dir(stagedAbs) != filepath.Dir(targetAbs) {
		return fmt.Errorf("staged graph must share target directory for atomic publication")
	}
	for _, sidecar := range []string{stagedAbs + "-wal", stagedAbs + "-shm"} {
		if err := os.Remove(sidecar); err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("remove staged sidecar %s: %w", filepath.Base(sidecar), err)
		}
	}
	if err := replacePublishedFile(stagedAbs, targetAbs); err != nil {
		return fmt.Errorf("atomic replace %s: %w", targetAbs, err)
	}
	return nil
}
