package resolver

import (
	"fmt"
	"reflect"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// RED boundary for HAR-42 step 5 (VTA). The accepted CHA/RTA contract does
// not yet expose a variable-type analysis entry point. The future entry point
// is expected to be:
//
//	AnalyzeVTA(calls, meta, implements, assignments) []VTAResult
//
// VTA must propagate variable/return/field type facts into the candidate set,
// keep unrelated same-named methods out, retain every viable alternative, and
// leave selection empty when flow evidence is ambiguous. Reflection keeps this
// boundary independent of the concrete result type while the API is RED.
func TestVTAVariableFlowRetainsAlternativesAndAbstainsAmbiguity(t *testing.T) {
	meta := map[int64]NodeMeta{
		1:  {Label: "Function", Name: "main", File: "main.go"},
		2:  {Label: "Function", Name: "makeRunner", File: "factory.go", ReturnType: "Runner"},
		3:  {Label: "Interface", Name: "Runner", File: "runner.go"},
		4:  {Label: "Struct", Name: "ImplA", File: "a.go"},
		5:  {Label: "Struct", Name: "ImplB", File: "b.go"},
		6:  {Label: "Interface", Name: "Unrelated", File: "unrelated.go"},
		7:  {Label: "Struct", Name: "UnrelatedImpl", File: "u.go"},
		8:  {Label: "Method", Name: "Run", ParentID: 4, File: "a.go"},
		9:  {Label: "Method", Name: "Run", ParentID: 5, File: "b.go"},
		10: {Label: "Method", Name: "Run", ParentID: 7, File: "u.go"},
		11: {Label: "Interface", Name: "Client", File: "client.go"},
		12: {Label: "Struct", Name: "ClientImpl", File: "client_impl.go"},
		13: {Label: "Method", Name: "Run", ParentID: 12, File: "client_impl.go"},
	}
	implements := map[int64][]int64{4: {3}, 5: {3}, 7: {6}, 12: {11}}

	t.Run("same_scope_assignment_is_ambiguous", func(t *testing.T) {
		results := AnalyzeVTA(
			[]parser.CallRef{{
				CalleeName: "Run", CalleeQualified: "runner.Run", File: "main.go", Line: 20,
				DispatchForm: "interface",
			}},
			meta,
			implements,
			[]parser.AssignmentRef{
				{VarName: "runner", TypeName: "ImplA", Scope: "main", File: "main.go", Line: 8},
				{VarName: "runner", TypeName: "ImplB", Scope: "main", File: "main.go", Line: 9},
				// A same-named method in an unrelated interface must not leak in.
				{VarName: "runner", TypeName: "UnrelatedImpl", Scope: "dead", File: "dead.go", Line: 2},
			},
		)
		assertVTAField(t, results, 0, "CandidateNodeIDs", "[8 9]")
		assertVTANilField(t, results, 0, "SelectedTargetNodeID")
	})

	t.Run("return_type_flow_is_retained", func(t *testing.T) {
		results := AnalyzeVTA(
			[]parser.CallRef{
				{CalleeName: "makeRunner", CalleeQualified: "makeRunner", File: "main.go", Line: 7, DispatchForm: "static"},
				{CalleeName: "Run", CalleeQualified: "runner.Run", File: "main.go", Line: 20, DispatchForm: "interface"},
			},
			meta,
			implements,
			[]parser.AssignmentRef{{
				VarName: "runner", TypeName: "makeRunner", Scope: "main", File: "main.go", Line: 8,
				ViaReturn: true,
			}},
		)
		assertVTAField(t, results, 1, "CandidateNodeIDs", "[8 9]")
		assertVTANilField(t, results, 1, "SelectedTargetNodeID")
	})

	t.Run("cross_method_field_flow_is_retained", func(t *testing.T) {
		results := AnalyzeVTA(
			[]parser.CallRef{{
				CalleeName: "Run", CalleeQualified: "self.client.Run", File: "service.go", Line: 30,
				DispatchForm: "interface",
			}},
			meta,
			implements,
			[]parser.AssignmentRef{{
				VarName: "self.client", TypeName: "Client", Scope: "__init__", File: "service.go", Line: 5,
			}},
		)
		assertVTAField(t, results, 0, "CandidateNodeIDs", "[13]")
	})
}

func assertVTAField(t *testing.T, results any, index int, field, want string) {
	t.Helper()
	value := vtaResultField(t, results, index, field)
	if got := fmt.Sprint(value.Interface()); got != want {
		t.Fatalf("%s=%s, want %s", field, got, want)
	}
}

func assertVTANilField(t *testing.T, results any, index int, field string) {
	t.Helper()
	value := vtaResultField(t, results, index, field)
	if !value.IsNil() {
		t.Fatalf("%s=%v, want nil for ambiguous flow", field, value.Interface())
	}
}

func vtaResultField(t *testing.T, results any, index int, field string) reflect.Value {
	t.Helper()
	items := reflect.ValueOf(results)
	if items.Kind() != reflect.Slice || index >= items.Len() {
		t.Fatalf("VTA result is not a slice with index %d: %T", index, results)
	}
	item := items.Index(index)
	if item.Kind() == reflect.Pointer {
		item = item.Elem()
	}
	value := item.FieldByName(field)
	if !value.IsValid() {
		t.Fatalf("VTA result has no %s field: %s", field, item.Type())
	}
	return value
}
