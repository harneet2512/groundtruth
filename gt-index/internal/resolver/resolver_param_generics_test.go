package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// A5 (Fable 2026-07-05): BuildParamTypeIndex truncated a param annotation at the FIRST '['
// (`user: Optional[User]` → `Optional`), leaving rung 1.94a dead for every generic-typed
// param — receiverTypeName("Optional") is a nonexistent class, so the call fell to name_match.
// The fix mirrors BuildFieldTypeIndex: strip only a trailing space-introduced flag, keep the
// generic for receiverTypeName→stripTypeWrapper (Optional[User]→User) at resolve time.
//
// RED (revert the strip guard): the param type is "Optional"; 1.94a misses; user.save() resolves
// to name_match (Method != "type_flow"), and the decoy Widget.save (id 4) is an equally-valid
// name_match target. GREEN: type_flow → User.save (id 3), conf 0.9.
func TestResolve_A5_GenericParamTypeResolvesReceiver(t *testing.T) {
	fm := BuildFileMap([]string{"svc.py", "model.py", "widget.py"},
		[]string{"python", "python", "python"})
	nodeIDs := map[string][]int64{
		"save_user": {1}, "User": {10}, "Widget": {20}, "save": {3, 4},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"svc.py":    {"save_user": {1}},
		"model.py":  {"User": {10}, "save": {3}},
		"widget.py": {"Widget": {20}, "save": {4}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "save_user", File: "svc.py", StartLine: 1},
		10: {Label: "Class", Name: "User", File: "model.py", StartLine: 1},
		3:  {Label: "Method", Name: "save", File: "model.py", ParentID: 10, StartLine: 2},
		20: {Label: "Class", Name: "Widget", File: "widget.py", StartLine: 1},
		4:  {Label: "Method", Name: "save", File: "widget.py", ParentID: 20, StartLine: 2},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "save", CalleeQualified: "user.save", Line: 2, File: "svc.py"},
	}
	// save_user(user: Optional[User]) — the param's declared type is a generic wrapper.
	props := []parser.PropertyRef{
		{Kind: "param", NodeIdx: 0, Value: "user:Optional[User]"},
	}
	SetParamTypeIndex(BuildParamTypeIndex(props, []int64{1}))
	defer SetParamTypeIndex(nil)

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, []int64{1}, nil, fm, meta)
	var edge *ResolvedCall
	for i := range resolved {
		if resolved[i].SourceNodeID == 1 &&
			(resolved[i].TargetNodeID == 3 || resolved[i].TargetNodeID == 4) {
			edge = &resolved[i]
		}
	}
	if edge == nil {
		t.Fatalf("user.save() did not resolve to any edge; resolved=%+v", resolved)
	}
	if edge.Method != "type_flow" || edge.TargetNodeID != 3 {
		t.Errorf("user.save() resolved to id %d via %s (conf %.2f); want User.save id 3 via type_flow "+
			"(the generic param type Optional[User] must survive indexing → receiverTypeName → User)",
			edge.TargetNodeID, edge.Method, edge.Confidence)
	}
}
