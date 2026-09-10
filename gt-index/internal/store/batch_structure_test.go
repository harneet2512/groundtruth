package store

import (
	"path/filepath"
	"testing"
)

func TestBatchStructureRejectsTamperedRowsAndRollsBack(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	node := &Node{Label: "Function", Name: "work", FilePath: "work.py", Language: "python", FileHash: "source"}
	ids, _, err := db.ReplaceParsedStructure([]*Node{node}, false, "")
	if err != nil {
		t.Fatal(err)
	}
	receipt := completeCoreReceipt()
	payload, digest, err := receipt.Seal()
	if err != nil {
		t.Fatal(err)
	}
	for key, value := range map[string]string{CorePhaseReceiptKey: payload, CorePhaseReceiptSHA256Key: digest} {
		if err := db.SetMeta(key, value); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := db.db.Exec(`UPDATE nodes SET signature='tampered' WHERE id=?`, ids[0]); err != nil {
		t.Fatal(err)
	}
	if _, _, err := db.ReplaceParsedStructure([]*Node{node}, true, receipt.ExecutableSHA256); err == nil {
		t.Fatal("tampered source row was retained")
	}
	if got := metaValueOrEmpty(db.db, CorePhaseReceiptSHA256Key); got != digest {
		t.Fatal("failed amendment did not roll back metadata")
	}
	if db.NodeCount() != 1 {
		t.Fatal("failed amendment did not roll back nodes")
	}
}

func TestBatchStructureRejectsMissingInventory(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	receipt := completeCoreReceipt()
	payload, digest, err := receipt.Seal()
	if err != nil {
		t.Fatal(err)
	}
	for key, value := range map[string]string{CorePhaseReceiptKey: payload, CorePhaseReceiptSHA256Key: digest} {
		if err := db.SetMeta(key, value); err != nil {
			t.Fatal(err)
		}
	}
	if _, _, err := db.ReplaceParsedStructure(nil, true, receipt.ExecutableSHA256); err == nil {
		t.Fatal("legacy parent accepted without inventory")
	}
	if _, _, err := db.ReplaceParsedStructure(nil, true, "other-producer"); err == nil {
		t.Fatal("foreign producer accepted")
	}
}
