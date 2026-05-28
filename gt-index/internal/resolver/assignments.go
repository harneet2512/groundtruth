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
	TypeName  string // "SomeClass", "HttpClient"
	TypeFile  string // file where type is defined (empty = same file or unknown)
	Scope     string // function name where assignment occurred (empty = module level)
	Line      int    // line number of the assignment
	Confident bool   // true if assignment is unambiguous (direct constructor call)
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

// Lookup returns the type(s) for a variable. Returns nil if unknown.
func (m *AssignmentMap) Lookup(varName string) []VarType {
	return m.VarTypes[varName]
}

// ResolveQualifiedCall attempts to resolve a qualified call like x.method()
// using the assignment map. Returns (targetClassName, methodName, found).
//
// Example: x = HttpClient(); x.get() → ("HttpClient", "get", true)
func (m *AssignmentMap) ResolveQualifiedCall(qualifier string, method string) (string, string, bool) {
	types := m.Lookup(qualifier)
	if len(types) == 0 {
		return "", "", false
	}
	// Pick the most confident (and latest) assignment
	best := types[len(types)-1]
	if !best.Confident && len(types) > 1 {
		for _, t := range types {
			if t.Confident {
				best = t
				break
			}
		}
	}
	return best.TypeName, method, true
}
