package community

import (
	"reflect"
	"testing"
)

func TestReboundEvidenceReCitesCallIDsInBuildOrder(t *testing.T) {
	members := []string{"c.py", "a.py", "b.py"}
	old := []string{"1", "4", "cochange:a.py|b.py", "7"}
	byPair := map[[2]string][]int64{
		{"a.py", "b.py"}: {40, 12},
		{"b.py", "c.py"}: {90},
	}
	for k := range byPair {
		ids := byPair[k]
		if ids[0] > ids[len(ids)-1] {
			ids[0], ids[len(ids)-1] = ids[len(ids)-1], ids[0]
		}
	}
	got, err := reboundEvidence(old, false, members, byPair)
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"12", "40", "cochange:a.py|b.py", "90"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("rebound=%v want %v", got, want)
	}
}

func TestReboundEvidenceRefusesAChangedShape(t *testing.T) {
	members := []string{"a.py", "b.py"}
	// Carried list cites two calls; the amended graph has one: the partition
	// input changed, so the evidence cannot be re-cited — rebuild instead.
	if _, err := reboundEvidence([]string{"1", "2"}, false, members, map[[2]string][]int64{{"a.py", "b.py"}: {5}}); err == nil {
		t.Fatal("a changed evidence shape must be refused")
	}
	// Truncated lists are cut at the carried length.
	got, err := reboundEvidence([]string{"1"}, true, members, map[[2]string][]int64{{"a.py", "b.py"}: {5, 6}})
	if err != nil || !reflect.DeepEqual(got, []string{"5"}) {
		t.Fatalf("truncated rebind = %v, %v", got, err)
	}
}
