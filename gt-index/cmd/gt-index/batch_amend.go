package main

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// copyBatchParent accepts only a checkpointed, self-contained graph. It never opens
// the parent with a writable SQLite connection or replaces its published path.
func copyBatchParent(parent, staged, target string) error {
	parentAbs, err := filepath.Abs(parent)
	if err != nil {
		return err
	}
	targetAbs, err := filepath.Abs(target)
	if err != nil {
		return err
	}
	info, err := os.Stat(parentAbs)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("batch parent is not a regular file")
	}
	if targetInfo, err := os.Stat(targetAbs); err == nil && os.SameFile(info, targetInfo) {
		return fmt.Errorf("batch output must not replace its parent")
	}
	if parentAbs == targetAbs {
		return fmt.Errorf("batch output must differ from parent")
	}
	for _, suffix := range []string{"-wal", "-journal"} {
		// Read-only SQLite consumers may leave an empty WAL and shared-memory
		// index. Only a nonempty data sidecar makes copying the main file unsafe.
		if sidecar, err := os.Stat(parentAbs + suffix); !os.IsNotExist(err) && (err != nil || sidecar.Size() != 0) {
			return fmt.Errorf("batch parent is not checkpointed: %s", suffix)
		}
	}
	in, err := os.Open(parentAbs)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(staged, os.O_WRONLY|os.O_TRUNC, 0600)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	if err := out.Sync(); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}
