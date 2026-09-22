package parser

import (
	"strings"

	sitter "github.com/smacker/go-tree-sitter"
)

// receiverCallInfo extracts (callee, qualified) for the call shapes whose
// method name and receiver are NOT carried by a member expression in the
// call's first child. handled=false means the shape is not one of these and
// the generic extractCalleeInfo path applies.
//
// Every receiver call yields callee = the METHOD name and qualified =
// "<receiver>.<method>" ("<Scope>::<method>" for a PHP/Ruby scope-resolution
// call), the same (simple, qualified) contract Python/Go/JS/TS receiver calls
// have, so the parser marks them "virtual" and the resolver's receiver rungs
// (self/this, declared type, Class.m, assignment flow) apply uniformly.
//
// Node shapes (tree-sitter field names verified per grammar):
//
//	Java   method_invocation            object: <expr>  name: identifier
//	Ruby   call                         receiver: <expr> operator: "."|"&."|"::" method: identifier
//	PHP    member_call_expression       object: <expr>  name: name
//	PHP    nullsafe_member_call_expression (same fields, `?->`)
//	PHP    scoped_call_expression       scope: <name>   name: name
//	C#     invocation_expression        function: member_access_expression{expression, name}
//	Kotlin call_expression              navigation_expression{<expr>, navigation_suffix{simple_identifier}}
//	Swift  call_expression              navigation_expression{target: <expr>, suffix: navigation_suffix{suffix: simple_identifier}}
func receiverCallInfo(callNode *sitter.Node, src []byte) (simple, qualified string, handled bool) {
	switch callNode.Type() {
	case "method_invocation": // Java
		name := callNode.ChildByFieldName("name")
		if name == nil {
			return "", "", false
		}
		simple = strings.TrimSpace(name.Content(src))
		obj := callNode.ChildByFieldName("object")
		if obj == nil {
			return simple, simple, true
		}
		if receiverRootIsLiteral(obj, 0) {
			return "", "", true
		}
		return simple, strings.TrimSpace(obj.Content(src)) + "." + simple, true

	case "call": // Ruby (Python's `call` has a `function` field, never `method`)
		method := callNode.ChildByFieldName("method")
		if method == nil {
			return "", "", false
		}
		simple = strings.TrimSpace(method.Content(src))
		recv := callNode.ChildByFieldName("receiver")
		if recv == nil {
			return simple, simple, true
		}
		if receiverRootIsLiteral(recv, 0) {
			return "", "", true
		}
		sep := "."
		if op := callNode.ChildByFieldName("operator"); op != nil && strings.TrimSpace(op.Content(src)) == "::" {
			sep = "::"
		}
		return simple, strings.TrimSpace(recv.Content(src)) + sep + simple, true

	case "member_call_expression", "nullsafe_member_call_expression": // PHP $x->m()
		name := callNode.ChildByFieldName("name")
		obj := callNode.ChildByFieldName("object")
		if name == nil || obj == nil {
			return "", "", false
		}
		simple = strings.TrimSpace(name.Content(src))
		return simple, phpReceiverText(obj.Content(src)) + "." + simple, true

	case "scoped_call_expression": // PHP Foo::bar() / self::bar() / static::bar()
		name := callNode.ChildByFieldName("name")
		scope := callNode.ChildByFieldName("scope")
		if name == nil || scope == nil {
			return "", "", false
		}
		simple = strings.TrimSpace(name.Content(src))
		sc := strings.TrimSpace(scope.Content(src))
		if i := strings.LastIndex(sc, `\`); i >= 0 {
			sc = sc[i+1:] // \App\Models\User::find → User
		}
		if sc == "static" {
			sc = "self" // late static binding still dispatches on the caller's class
		}
		return simple, sc + "::" + simple, true

	case "invocation_expression": // C#
		fn := callNode.ChildByFieldName("function")
		if fn == nil {
			return "", "", false
		}
		switch fn.Type() {
		case "member_access_expression":
			expr := fn.ChildByFieldName("expression")
			name := fn.ChildByFieldName("name")
			if expr == nil || name == nil {
				return "", "", false
			}
			simple = csharpSimpleName(name, src)
			if simple == "" {
				return "", "", false
			}
			if receiverRootIsLiteral(expr, 0) {
				return "", "", true
			}
			return simple, strings.TrimSpace(expr.Content(src)) + "." + simple, true
		case "generic_name": // Foo<T>(x)
			simple = csharpSimpleName(fn, src)
			if simple == "" {
				return "", "", false
			}
			return simple, simple, true
		}
		return "", "", false

	case "call_expression": // Kotlin / Swift navigation calls
		nav := callNode.Child(0)
		if nav == nil || nav.Type() != "navigation_expression" {
			return "", "", false
		}
		recv, method := navigationParts(nav)
		if recv == nil || method == nil {
			return "", "", false
		}
		if receiverRootIsLiteral(recv, 0) {
			return "", "", true
		}
		simple = strings.TrimSpace(method.Content(src))
		return simple, strings.TrimSpace(recv.Content(src)) + "." + simple, true
	}
	return "", "", false
}

// phpReceiverText normalizes a PHP receiver expression to the dotted,
// sigil-free spelling the resolver's receiver rungs key on:
// `$this` → "this", `$this->repo` → "this.repo", `$x?->y` → "x.y".
func phpReceiverText(raw string) string {
	t := strings.TrimSpace(raw)
	t = strings.ReplaceAll(t, "?->", ".")
	t = strings.ReplaceAll(t, "->", ".")
	t = strings.ReplaceAll(t, "$", "")
	return t
}

// csharpSimpleName returns the identifier of a C# simple name node:
// `identifier` itself, or the identifier inside `generic_name` (`Run<T>`).
func csharpSimpleName(n *sitter.Node, src []byte) string {
	switch n.Type() {
	case "identifier":
		return strings.TrimSpace(n.Content(src))
	case "generic_name":
		for i := 0; i < int(n.ChildCount()); i++ {
			if c := n.Child(i); c != nil && c.Type() == "identifier" {
				return strings.TrimSpace(c.Content(src))
			}
		}
	}
	return ""
}

// navigationParts splits a Kotlin/Swift navigation_expression into its
// receiver expression and the member identifier of its navigation_suffix.
// Swift exposes them on `target`/`suffix` fields; Kotlin has no fields, so
// the receiver is the first named child and the suffix the last.
func navigationParts(nav *sitter.Node) (recv, method *sitter.Node) {
	recv = nav.ChildByFieldName("target")
	suffix := nav.ChildByFieldName("suffix")
	if recv == nil || suffix == nil {
		named := int(nav.NamedChildCount())
		if named < 2 {
			return nil, nil
		}
		recv = nav.NamedChild(0)
		suffix = nav.NamedChild(named - 1)
	}
	if suffix == nil || suffix.Type() != "navigation_suffix" {
		return nil, nil
	}
	method = suffix.ChildByFieldName("suffix")
	if method == nil {
		for i := int(suffix.NamedChildCount()) - 1; i >= 0; i-- {
			if c := suffix.NamedChild(i); c != nil && c.Type() == "simple_identifier" {
				method = c
				break
			}
		}
	}
	if method == nil || method.Type() != "simple_identifier" {
		return nil, nil
	}
	return recv, method
}

// jvmFunctionalTypes are the JDK single-abstract-method interfaces whose
// instances are callable values. A formal typed by one of them holds a lambda
// or method reference; calling its SAM method invokes that callable.
var jvmFunctionalTypes = map[string]bool{
	"Runnable": true, "Callable": true, "Supplier": true, "Consumer": true,
	"BiConsumer": true, "Function": true, "BiFunction": true, "Predicate": true,
	"BiPredicate": true, "UnaryOperator": true, "BinaryOperator": true,
}

// jvmFunctionalParamType reports whether a Java/Kotlin formal's normalized
// declared type makes it a callable value: a JDK functional interface
// (including primitive specializations such as IntFunction/ToLongFunction),
// or "" — a Kotlin function type (`cb: () -> Unit`) normalizes to no class.
func jvmFunctionalParamType(typeName string) bool {
	if typeName == "" {
		return true
	}
	if jvmFunctionalTypes[typeName] {
		return true
	}
	for _, prefix := range []string{"Int", "Long", "Double", "ToInt", "ToLong", "ToDouble", "Obj"} {
		if rest := strings.TrimPrefix(typeName, prefix); rest != typeName && jvmFunctionalTypes[rest] {
			return true
		}
	}
	return false
}

// jvmCallableReceiverCall reports whether a Java/Kotlin receiver call
// (`r.run()`, `this.r.run()`, `cb.apply(x)`) invokes a callable VALUE: its
// receiver is a callable-alias field of the caller's class, a callable alias
// (method/callable reference) visible in the caller's scope or at module
// scope, or a functional-typed formal. The SAM method name is irrelevant —
// the receiver binding is the dispatch.
func jvmCallableReceiverCall(c *CallRef, fields, samParams, aliases map[string]map[string]struct{}) bool {
	q := c.CalleeQualified
	dot := strings.LastIndex(q, ".")
	if dot <= 0 {
		return false
	}
	recv := q[:dot]
	callerClass := c.CallerScope
	if i := strings.LastIndex(callerClass, "."); i >= 0 {
		callerClass = callerClass[:i]
	}
	has := func(m map[string]map[string]struct{}, scope string) bool {
		if inner, ok := m[scope]; ok {
			_, ok := inner[recv]
			return ok
		}
		return false
	}
	return (callerClass != "" && has(fields, callerClass)) ||
		has(samParams, c.CallerScope) ||
		has(aliases, c.CallerScope) || has(aliases, "")
}
