package resolver

import "strings"

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
	TypeName  string // "SomeClass", "HttpClient" — callee name (ViaReturn) or RHS symbol leaf (ViaSymbol)
	TypeFile  string // file where type is defined (empty = same file or unknown)
	Scope     string // function name where assignment occurred (empty = module level)
	Line      int    // line number of the assignment
	Confident bool   // true if assignment is unambiguous (direct constructor call)
	// ViaReturn marks an assignment whose RHS is a (non-constructor) call: x = factory().
	// TypeName then holds the CALLEE name, not a class; the resolver must bridge through
	// that callee's declared return type (Strat 1.96 viaReturn) before resolving the method.
	// JARVIS (arXiv 2305.05949): return-type chaining is a core flow-sensitive transfer.
	ViaReturn bool
	// ViaSymbol marks a callable-value binding whose RHS is a symbol reference:
	// `x = helper`, `x = obj.method`, `x = mod.func`, `self.f = F`. TypeName is
	// the RHS leaf name; TypeQualified the full source text ("obj.method"). The
	// callable-value rung resolves `x()` to the named symbol.
	ViaSymbol bool
	// IsParameter marks a formal parameter binding (JARVIS argument→formal
	// flow): TypeName is the declared annotation (empty when unannotated — the
	// parameter is then a pure value slot), ParameterIndex its positional slot
	// excluding receiver formals (self/cls).
	IsParameter    bool
	ParameterIndex int
	// Owner names the function/method a formal parameter belongs to
	// (IsParameter only) — the callsite key the callable-value rung scans.
	// It differs from Scope when the owning function is nested inside another
	// body (`const w = (cb) => …` inside `outer`: Scope="outer", Owner="w").
	Owner string
	// TypeQualified is the qualified RHS/annotation text ("mod.func",
	// "pkg.Type") when the source carried one.
	TypeQualified string
	// ObjectScope is the enclosing class/object for self./this. field bindings —
	// the object-scope key the field path and callable-value rung match on.
	ObjectScope string
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

// ResolveCallableBinding resolves the binding behind a callee that is a
// function VALUE, not a declared symbol — the callsite `x()` or `self.cb()`
// where `x`/`self.cb` was bound by an alias write (`x = helper`,
// `x = obj.method`) or is a formal parameter (`def wrap(cb): cb()`).
//
// varName is the callsite's callee (bare "x" or qualified "self.cb"); scope is
// the caller's enclosing function scope; objectScope the caller's enclosing
// class (for field bindings); line the callsite line. Returns the single
// viable binding, or found=false when the name has no callable binding, the
// latest write is not a symbol/parameter binding, or multiple viable bindings
// disagree — ambiguity abstains rather than guesses.
//
// Scope rules mirror ResolveQualifiedCall: self./this. fields are
// object-scoped (any method's write is eligible when the class matches);
// dotted non-field names and bare names are function-scoped (same-scope
// writes, or module-level Scope=="" globals). For non-field names a write
// textually AFTER the call (use-before-assign) is ineligible. A formal
// parameter is chosen only when no later write shadows it — a rebound param
// (`cb = helper` inside the body) resolves through the write.
func (m *AssignmentMap) ResolveCallableBinding(varName, scope, objectScope string, line int) (VarType, bool) {
	types := m.VarTypes[varName] // exact name — Lookup's self./this. fallback would merge distinct slots
	if len(types) == 0 {
		return VarType{}, false
	}
	field := strings.HasPrefix(varName, "self.") || strings.HasPrefix(varName, "this.")
	dotted := !field && strings.ContainsAny(varName, ".:")
	var eligible []VarType
	for _, t := range types {
		switch {
		case field:
			// Object-scoped: writes on the same class (or an unscoped write)
			// are eligible from every method.
			if t.ObjectScope == "" || objectScope == "" || t.ObjectScope == objectScope {
				eligible = append(eligible, t)
			}
		case dotted:
			// A dotted name is function-scoped; a binding that also carries an
			// ObjectScope (Java/Kotlin field alias) must come from the SAME
			// class — a sibling class's `this.f` must never bind `f.run()`.
			if t.Scope == scope && (t.ObjectScope == "" || t.ObjectScope == objectScope) {
				eligible = append(eligible, t)
			}
		default:
			// Bare names: same-scope writes and module-level globals are
			// eligible, but an ObjectScope-carrying binding (the bare twin of
			// a Java/Kotlin `this.f` field alias, Scope=="") only applies
			// inside its own class — a module-level or sibling-class call
			// cannot see it.
			if (t.Scope == scope || t.Scope == "") &&
				(t.ObjectScope == "" || t.ObjectScope == objectScope) {
				eligible = append(eligible, t)
			}
		}
	}
	// A local write textually after the call cannot be the binding it reads
	// (use-before-assign). Fields are exempt — methods are unordered.
	if !field && line > 0 {
		var filtered []VarType
		for _, t := range eligible {
			if t.Line <= line || t.Line == 0 {
				filtered = append(filtered, t)
			}
		}
		eligible = filtered
	}
	if len(eligible) == 0 {
		return VarType{}, false
	}
	var params, writes []VarType
	for _, t := range eligible {
		if t.IsParameter {
			params = append(params, t)
		} else {
			writes = append(writes, t)
		}
	}
	if len(writes) == 0 {
		// Pure parameter binding: viable only when every same-named formal
		// agrees on position AND owner (a redefined function with a moved
		// formal — or two same-scoped nested functions declaring the same
		// formal — is ambiguous).
		if len(params) == 1 {
			return params[0], true
		}
		first := params[0]
		for _, p := range params[1:] {
			if p.ParameterIndex != first.ParameterIndex || p.VarName != first.VarName || p.Owner != first.Owner {
				return VarType{}, false
			}
		}
		return first, true
	}
	// Writes exist: the latest write decides what the name currently holds.
	latest := 0
	for _, t := range writes {
		if t.Line > latest {
			latest = t.Line
		}
	}
	var latestSymbol *VarType
	for i := range writes {
		if writes[i].Line == latest && writes[i].ViaSymbol {
			latestSymbol = &writes[i]
		}
	}
	if latestSymbol == nil {
		// Latest write is a constructor/factory binding — `x` holds a value of
		// a type, not a callable symbol: `x()` is not a callable-value call.
		return VarType{}, false
	}
	// Every ViaSymbol write must agree on the same target: a conditional
	// rebind (`if c: x = f else: x = g`) leaves both writes viable.
	target := latestSymbol.TypeName + "\x00" + latestSymbol.TypeQualified
	for _, t := range writes {
		if !t.ViaSymbol {
			continue
		}
		if t.TypeName+"\x00"+t.TypeQualified != target {
			return VarType{}, false
		}
	}
	return *latestSymbol, true
}
