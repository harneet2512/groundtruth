package main

import (
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

func TestRequiredMetadataWriteFailureIsFatalToPublication(t *testing.T) {
	db, err := store.Open(filepath.Join(t.TempDir(), "closed.db"))
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	if err := setRequiredMetadata(db, map[string]string{"parse_failures": "0"}); err == nil {
		t.Fatal("required metadata write on closed database unexpectedly succeeded")
	}
}
