package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// ----------------------------------------------------------------------------
// ReceiverTypeNorm suite — Fable P0-A (2026-07-05).
//
// receiverTypeName is wired at the 8 RECEIVER-POSITION sites that previously
// called stripTypeWrapper. stripTypeWrapper unwraps a container/generic/union to
// its ELEMENT type (`Dict[str,Entry]`→`Entry`, `Queue[Task]`→`Task`, `Foo|Bar`→
// `Foo`); when that element was then used as the receiver CLASS, the resolver
// minted a CERTIFIED edge onto the WRONG class (the stdlib-shadow launder — e.g.
// `d.get()` where `d: Dict[str,Entry]` resolving to an internal `Entry.get`).
//
// receiverTypeName honors the doc's abstain contract instead:
//   · builtin CONTAINER head (List/Dict/Vec/Map/[]T/map[..]) → ABSTAIN (the
//     receiver is a builtin, there is no internal element class to resolve);
//   · CUSTOM generic (`Queue[Task]`) → the HEAD (`Queue`) is the receiver class,
//     never the type argument;
//   · UNION with >=2 non-None arms → ABSTAIN (ambiguous receiver).
//
// These cases exercise the wiring through Strategy 2b's declared-FIELD path (the
// site reverted in the red-proof). Each called method is defined on TWO classes so
// no unique-method shortcut fires and 2b is the deciding rung — mirroring the
// discriminator design in resolver_fieldtype_test.go. Data tables are
// language-uniform; nothing here keys on a repo/task/gold/FAIL_TO_PASS.
// ----------------------------------------------------------------------------

// (1) CONTAINER ABSTAIN: field `self.items: Dict[str, Entry]` + `self.items.fetch()`.
// `fetch` is defined on TWO classes (Entry id 4, Record id 6), so no unique-method
// shortcut fires; 2b decides. receiverTypeName("Dict[str, Entry]") = ("", true) —
// the receiver is the builtin dict, NOT the value type Entry — so NO receiver-proven
// (field_type / type_flow / CERTIFIED) edge is minted onto Entry.fetch. Under the old
// stripTypeWrapper the Dict VALUE type Entry was extracted and Entry.fetch was
// laundered as a CERTIFIED field_type fact (the bug).
func TestReceiverTypeNorm_ContainerFieldAbstains(t *testing.T) {
	files := []string{"svc.py", "entry.py", "record.py"}
	langs := []string{"python", "python", "python"}
	fm := BuildFileMap(files, langs)

	nodeIDs := map[string][]int64{
		"handler": {1}, "Service": {10},
		"Entry": {3}, "Record": {5}, "fetch": {4, 6},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"svc.py":    {"handler": {1}, "Service": {10}},
		"entry.py":  {"Entry": {3}, "fetch": {4}},
		"record.py": {"Record": {5}, "fetch": {6}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", File: "svc.py", Name: "handler", ParentID: 10},
		10: {Label: "Class", File: "svc.py", Name: "Service"},
		3:  {Label: "Class", File: "entry.py", Name: "Entry"},
		4:  {Label: "Method", File: "entry.py", Name: "fetch", ParentID: 3},
		5:  {Label: "Class", File: "record.py", Name: "Record"},
		6:  {Label: "Method", File: "record.py", Name: "fetch", ParentID: 5},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "fetch", CalleeQualified: "self.items.fetch", Line: 5, File: "svc.py"},
	}
	callerIDs := []int64{1}

	SetFieldTypeIndex(map[int64]map[string]string{10: {"items": "Dict[str, Entry]"}})
	defer SetFieldTypeIndex(nil)

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	for _, r := range resolved {
		if r.EvidenceType == "field_type" {
			t.Errorf("container element laundered: 2b minted a field_type edge from the Dict[str,Entry] element type: %+v", r)
		}
		if r.TargetNodeID == 4 && (r.Method == "type_flow" || r.TrustTier == "CERTIFIED") {
			t.Errorf("receiver-proven edge minted onto Entry.fetch(4) for a builtin-dict receiver (must abstain; name_match is fine): %+v", r)
		}
	}
}

// (2) CUSTOM-GENERIC HEAD: field `self.q: Queue[Task]` + `self.q.enqueue()`. Both
// Queue (id 3) and Task (id 5) are internal classes and BOTH define enqueue (ids 4,
// 6). receiverTypeName("Queue[Task]") = ("Queue", false) — the HEAD is the receiver
// class — so 2b resolves Queue.enqueue (id 4), NOT the type argument Task.enqueue
// (id 6). The old stripTypeWrapper took the type arg Task → id 6 (wrong class).
func TestReceiverTypeNorm_CustomGenericResolvesHead(t *testing.T) {
	files := []string{"svc.py", "queue.py", "task.py"}
	langs := []string{"python", "python", "python"}
	fm := BuildFileMap(files, langs)

	nodeIDs := map[string][]int64{
		"handler": {1}, "Service": {10},
		"Queue": {3}, "Task": {5}, "enqueue": {4, 6},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"svc.py":   {"handler": {1}, "Service": {10}},
		"queue.py": {"Queue": {3}, "enqueue": {4}},
		"task.py":  {"Task": {5}, "enqueue": {6}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", File: "svc.py", Name: "handler", ParentID: 10},
		10: {Label: "Class", File: "svc.py", Name: "Service"},
		3:  {Label: "Class", File: "queue.py", Name: "Queue"},
		4:  {Label: "Method", File: "queue.py", Name: "enqueue", ParentID: 3},
		5:  {Label: "Class", File: "task.py", Name: "Task"},
		6:  {Label: "Method", File: "task.py", Name: "enqueue", ParentID: 5},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "enqueue", CalleeQualified: "self.q.enqueue", Line: 5, File: "svc.py"},
	}
	callerIDs := []int64{1}

	SetFieldTypeIndex(map[int64]map[string]string{10: {"q": "Queue[Task]"}})
	defer SetFieldTypeIndex(nil)

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	if len(resolved) != 1 {
		t.Fatalf("expected 1 resolved edge, got %d: %+v", len(resolved), resolved)
	}
	r := resolved[0]
	if r.EvidenceType != "field_type" || r.Method != "type_flow" {
		t.Errorf("got method=%q evidence=%q, want type_flow/field_type (receiver-proven on the generic head)", r.Method, r.EvidenceType)
	}
	if r.TargetNodeID != 4 {
		t.Errorf("target = %d, want 4 (Queue.enqueue — the generic HEAD); NOT 6 (Task.enqueue, the type argument)", r.TargetNodeID)
	}
}

// (3) UNION ABSTAIN: field `self.x: User | Order` + `self.x.persist()`. Both User
// (id 3) and Order (id 5) are internal and BOTH define persist (ids 4, 6). A union
// with >=2 non-None arms is an ambiguous receiver → receiverTypeName = ("", true) →
// 2b abstains, no receiver-proven edge. The old stripTypeWrapper arbitrarily picked
// the first arm User → a CERTIFIED field_type edge on User.persist (id 4).
func TestReceiverTypeNorm_UnionFieldAbstains(t *testing.T) {
	files := []string{"svc.py", "user.py", "order.py"}
	langs := []string{"python", "python", "python"}
	fm := BuildFileMap(files, langs)

	nodeIDs := map[string][]int64{
		"handler": {1}, "Service": {10},
		"User": {3}, "Order": {5}, "persist": {4, 6},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"svc.py":   {"handler": {1}, "Service": {10}},
		"user.py":  {"User": {3}, "persist": {4}},
		"order.py": {"Order": {5}, "persist": {6}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", File: "svc.py", Name: "handler", ParentID: 10},
		10: {Label: "Class", File: "svc.py", Name: "Service"},
		3:  {Label: "Class", File: "user.py", Name: "User"},
		4:  {Label: "Method", File: "user.py", Name: "persist", ParentID: 3},
		5:  {Label: "Class", File: "order.py", Name: "Order"},
		6:  {Label: "Method", File: "order.py", Name: "persist", ParentID: 5},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "persist", CalleeQualified: "self.x.persist", Line: 5, File: "svc.py"},
	}
	callerIDs := []int64{1}

	SetFieldTypeIndex(map[int64]map[string]string{10: {"x": "User | Order"}})
	defer SetFieldTypeIndex(nil)

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	for _, r := range resolved {
		if r.EvidenceType == "field_type" {
			t.Errorf("union arbitrarily resolved: 2b minted a field_type edge from `User | Order`: %+v", r)
		}
		if (r.TargetNodeID == 4 || r.TargetNodeID == 6) && (r.Method == "type_flow" || r.TrustTier == "CERTIFIED") {
			t.Errorf("receiver-proven edge minted onto a union arm for an ambiguous receiver (must abstain): %+v", r)
		}
	}
}
