package resolver

// AssignmentTracker builds a per-file map of variable → type assignments.
// Used by Strategy 1.96 to resolve x.method() when x = SomeClass().
//
// PyCG (ICSE 2021): 13 state transition rules achieve 99% precision.
// JARVIS (2023): per-function scope, 84% higher precision, 82% recall.
//
// This implementation covers the 5 highest-impact rules:
//   Rule 1: x = ClassName()         → varTypes[x] = ClassName
//   Rule 2: x = module.ClassName()  → varTypes[x] = ClassName (via imports)
//   Rule 3: self.x = ClassName()    → attrTypes[self.x] = ClassName
//   Rule 4: x = func_call()         → varTypes[x] = return_type(func) if annotated
//   Rule 5: for x in collection     → varTypes[x] = element_type if inferable
//
// Rules 6-13 (closures, higher-order functions, dynamic features) are
// left for Step 2 / JARVIS-style flow analysis.

// VarType maps a variable name to its inferred class/type name and the
// file where that class is defined (for cross-file resolution).
type VarType struct {
	VarName   string // "x", "self.client", "result"
	TypeName  string // "SomeClass", "HttpClient" — or a callee name when ViaReturn
	TypeFile  string // file where type is defined (empty = same file or unknown)
	Scope     string // function name where assignment occurred (empty = module level)
	Line      int    // line number of the assignment
	Confident bool   // true if assignment is unambiguous (direct constructor call)
	// ViaReturn marks an assignment whose RHS is a (non-constructor) call: x = factory().
	// TypeName then holds the CALLEE name, not a class; the resolver must bridge through
	// that callee's declared return type (Strat 1.96b) before resolving the method.
	// JARVIS (arXiv 2305.05949): return-type chaining is a core flow-sensitive transfer.
	ViaReturn bool
}

// AssignmentMap is a per-file collection of variable → type inferences.
type AssignmentMap struct {
	VarTypes map[string][]VarType // variable name → possible types (usually 1)
}

// NewAssignmentMap creates an empty assignment map.
func NewAssignmentMap() *AssignmentMap {
	return &AssignmentMap{
		VarTypes: make(map[string][]VarType),
	}
}

// Add records a variable → type assignment.
func (m *AssignmentMap) Add(vt VarType) {
	m.VarTypes[vt.VarName] = append(m.VarTypes[vt.VarName], vt)
}

// Lookup returns the type(s) for a variable, tagging each with whether it came
// from a `self.`-prefixed (object-field) assignment. Returns nil if unknown.
// Handles both "x" and "self.x" forms — checks both.
//
// Field assignments (self.x = Foo()) are object-scoped: valid in ANY method of
// the class, so JARVIS treats them as cross-method facts. Local assignments
// (x = Foo()) are function-scoped: only valid in the function that wrote them.
func (m *AssignmentMap) Lookup(varName string) ([]VarType, bool) {
	if types := m.VarTypes[varName]; types != nil {
		return types, false
	}
	// Try with "self." prefix (Python: self.x = Foo() → lookup "x" finds "self.x")
	if types := m.VarTypes["self."+varName]; types != nil {
		return types, true
	}
	if types := m.VarTypes["this."+varName]; types != nil {
		return types, true
	}
	return nil, false
}

// ResolveQualifiedCall attempts to resolve a qualified call like x.method()
// using the assignment map. Returns (typeName, methodName, viaReturn, found).
//
// scope is the caller function's name (flow approximation, JARVIS per-procedure
// type graph). For a LOCAL var, an assignment whose Scope matches the caller is
// preferred over a same-named var typed in a different function (reduces the
// PyCG last-write-wins imprecision). For an object FIELD (self.x), the
// assignment is object-scoped so any method's write is eligible — the latest
// CONFIDENT one wins.
//
// Example: x = HttpClient(); x.get() → ("HttpClient", "get", false, true)
//          self.client = HttpClient() (in __init__); self.client.get() (elsewhere)
//            → ("HttpClient", "get", false, true)
func (m *AssignmentMap) ResolveQualifiedCall(qualifier, method, scope string) (string, string, bool, bool) {
	types, isField := m.Lookup(qualifier)
	if len(types) == 0 {
		return "", "", false, false
	}

	pick := func(eligible []VarType) (VarType, bool) {
		if len(eligible) == 0 {
			return VarType{}, false
		}
		// Latest assignment first (last-write-wins within the eligible set),
		// upgraded to the latest CONFIDENT one when an ambiguous later write exists.
		best := eligible[len(eligible)-1]
		if !best.Confident {
			for i := len(eligible) - 1; i >= 0; i-- {
				if eligible[i].Confident {
					best = eligible[i]
					break
				}
			}
		}
		return best, true
	}

	// Object fields are object-scoped — all writes are eligible regardless of scope.
	if isField {
		if best, ok := pick(types); ok {
			return best.TypeName, method, best.ViaReturn, true
		}
		return "", "", false, false
	}

	// Local var: prefer assignments written in the SAME function (flow scope).
	var inScope []VarType
	for _, t := range types {
		if scope != "" && t.Scope == scope {
			inScope = append(inScope, t)
		}
	}
	if best, ok := pick(inScope); ok {
		return best.TypeName, method, best.ViaReturn, true
	}
	// Fallback: no scope match (or scope unknown) → file-global last-write-wins,
	// preserving the prior behavior so no existing resolution regresses.
	if best, ok := pick(types); ok {
		return best.TypeName, method, best.ViaReturn, true
	}
	return "", "", false, false
}
