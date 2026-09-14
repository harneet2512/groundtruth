package store

import (
	"path/filepath"
	"testing"
)

func TestBatchStructureHealsADivergedInventoryEntry(t *testing.T) {
	// A parent mutated underneath parser_node_inventory — an LSP-merge UPDATE
	// that rewrote a signature in place — used to kill every later amend with
	// "batch parent parser row differs from its inventory". The diverged entry
	// is now treated as absent: the stale row is swept, the fresh parse is
	// minted under a new id, and the divergence is counted, not fatal.
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	node := &Node{Label: "Function", Name: "work", FilePath: "work.py", Language: "python", FileHash: "source"}
	ids, retained, diverged, err := db.ReplaceParsedStructure([]*Node{node}, false, "", false)
	if err != nil {
		t.Fatal(err)
	}
	if retained != 0 || diverged != 0 {
		t.Fatalf("fresh build retained=%d diverged=%d", retained, diverged)
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
	if _, err := db.db.Exec(`UPDATE nodes SET signature='enriched' WHERE id=?`, ids[0]); err != nil {
		t.Fatal(err)
	}
	ids2, retained, diverged, err := db.ReplaceParsedStructure([]*Node{node}, true, receipt.ExecutableSHA256, false)
	if err != nil {
		t.Fatal(err)
	}
	if diverged != 1 || retained != 0 {
		t.Fatalf("stale entry: retained=%d diverged=%d want 0/1", retained, diverged)
	}
	// The tampered content did not survive: one node row, fresh parse content.
	if db.NodeCount() != 1 {
		t.Fatalf("stale row was not swept: %d nodes", db.NodeCount())
	}
	var signature string
	if err := db.db.QueryRow(`SELECT COALESCE(signature,'') FROM nodes WHERE id=?`, ids2[0]).Scan(&signature); err != nil {
		t.Fatal(err)
	}
	if signature != "" {
		t.Fatalf("diverged row content retained: signature=%q", signature)
	}
	// The minted entry is self-consistent again: a second amend retains. The
	// pipeline rewrites project_meta on every build; the unit test re-seals
	// the receipt the way the next revision's core phase would.
	for key, value := range map[string]string{CorePhaseReceiptKey: payload, CorePhaseReceiptSHA256Key: digest} {
		if err := db.SetMeta(key, value); err != nil {
			t.Fatal(err)
		}
	}
	ids3, retained, diverged, err := db.ReplaceParsedStructure([]*Node{node}, true, receipt.ExecutableSHA256, false)
	if err != nil {
		t.Fatal(err)
	}
	if retained != 1 || diverged != 0 || ids3[0] != ids2[0] {
		t.Fatalf("healed inventory did not retain: ids=%v retained=%d diverged=%d", ids3, retained, diverged)
	}
}

func TestBatchStructureHealsADanglingInventoryEntry(t *testing.T) {
	// The other stale shape: the inventory names a rowid that no longer
	// exists at all (row deleted out-of-band). Same heal, same count.
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	node := &Node{Label: "Function", Name: "work", FilePath: "work.py", Language: "python", FileHash: "source"}
	if _, _, _, err := db.ReplaceParsedStructure([]*Node{node}, false, "", false); err != nil {
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
	// Delete the row but leave the inventory entry: a dangling alias.
	if _, err := db.db.Exec(`DELETE FROM nodes`); err != nil {
		t.Fatal(err)
	}
	_, retained, diverged, err := db.ReplaceParsedStructure([]*Node{node}, true, receipt.ExecutableSHA256, false)
	if err != nil {
		t.Fatal(err)
	}
	if retained != 0 || diverged != 1 {
		t.Fatalf("dangling entry: retained=%d diverged=%d want 0/1", retained, diverged)
	}
	if db.NodeCount() != 1 {
		t.Fatal("diverged node was not re-minted")
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
	if _, _, _, err := db.ReplaceParsedStructure(nil, true, receipt.ExecutableSHA256, false); err == nil {
		t.Fatal("legacy parent accepted without inventory")
	}
	if _, _, _, err := db.ReplaceParsedStructure(nil, true, "other-producer", false); err == nil {
		t.Fatal("foreign producer accepted")
	}
}
