package resolver

import (
	"strings"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// maxCallableAliasChainDepth bounds how many `g = f` copy hops the
// callable-value rung follows before abstaining.
const maxCallableAliasChainDepth = 8

// langFamily maps a node language onto the family inside which a call can
// bind a symbol by NAME. Languages that interoperate by name at the source
// level share a family (JS/TS/Svelte; Java/Kotlin/Scala/Groovy on the JVM;
// C/C++). "" (unknown) never filters.
func langFamily(lang string) string {
	switch lang {
	case "":
		return ""
	case "javascript", "typescript", "tsx", "jsx", "svelte":
		return "ecmascript"
	case "java", "kotlin", "scala", "groovy":
		return "jvm"
	case "c", "cpp":
		return "c"
	}
	return lang
}

// buildLanguageFamilyViews splits a name index into one view per language
// family, preserving each name's id order. A node whose language is unknown
// (no metadata) is kept in every view — the filter can only drop a node it
// KNOWS belongs to another family. Returns nil when no metadata is supplied.
func buildLanguageFamilyViews(nodeIDs map[string][]int64, meta map[int64]NodeMeta) map[string]map[string][]int64 {
	if meta == nil {
		return nil
	}
	families := make(map[string]bool)
	for _, m := range meta {
		if f := langFamily(m.Language); f != "" {
			families[f] = true
		}
	}
	views := make(map[string]map[string][]int64, len(families))
	for f := range families {
		views[f] = make(map[string][]int64)
	}
	for name, ids := range nodeIDs {
		for _, id := range ids {
			f := ""
			if m, ok := meta[id]; ok {
				f = langFamily(m.Language)
			}
			if f != "" {
				views[f][name] = append(views[f][name], id)
				continue
			}
			for fam := range views {
				views[fam][name] = append(views[fam][name], id)
			}
		}
	}
	return views
}

// buildExternalImportNames returns file → imported binding names that resolve
// to NO indexed file (`import requests`, `import subprocess`): the name binds
// code outside the repository.
func buildExternalImportNames(imports []parser.ImportRef, importIndex map[string]map[string][]string) map[string]map[string]bool {
	out := make(map[string]map[string]bool)
	for _, imp := range imports {
		if imp.ImportedName == "" || imp.ImportedName == "*" {
			continue
		}
		if len(importIndex[imp.File][imp.ImportedName]) > 0 {
			continue
		}
		m := out[imp.File]
		if m == nil {
			m = make(map[string]bool)
			out[imp.File] = m
		}
		m[imp.ImportedName] = true
	}
	return out
}

// receiverRoot returns the first segment of a qualified callee
// ("requests.adapters.get" → "requests", "Foo::bar" → "Foo").
func receiverRoot(qualified string) string {
	if i := strings.IndexAny(qualified, ".:"); i > 0 {
		return qualified[:i]
	}
	return ""
}

// splitQualifier splits a qualified callee into (receiver, member) on its
// last "." or "::" separator.
func splitQualifier(qualified string) (string, string) {
	dot := strings.LastIndex(qualified, ".")
	colon := strings.LastIndex(qualified, "::")
	switch {
	case dot > 0 && dot > colon:
		return qualified[:dot], qualified[dot+1:]
	case colon > 0:
		return qualified[:colon], qualified[colon+2:]
	}
	return "", qualified
}

// sameFileReceiverProven reports whether a QUALIFIED call's receiver is proven
// to own the unique same-file target, so the same_file rung may certify it:
//   - self/this/cls/Self receivers, when the target is a member of the
//     caller's own class;
//   - the Go method receiver variable (`r.M()` in `func (r *T)`), same rule;
//   - the owning class/type named directly (`Session.run()`, `Type::new()`).
//
// Anything else — a module, an untyped variable, a parameter, a call result —
// is unproven. Without metadata nothing can be proven.
func sameFileReceiverProven(qualified string, callerID, targetID int64, meta map[int64]NodeMeta) bool {
	if meta == nil {
		return false
	}
	target, ok := meta[targetID]
	if !ok || target.ParentID == 0 {
		return false
	}
	owner, ok := meta[target.ParentID]
	if !ok {
		return false
	}
	recv, _ := splitQualifier(qualified)
	if recv == "" {
		return false
	}
	caller := meta[callerID]
	switch recv {
	case "self", "this", "cls", "Self":
		return caller.ParentID == target.ParentID
	}
	if caller.ReceiverName != "" && recv == caller.ReceiverName {
		return caller.ParentID == target.ParentID
	}
	if owner.Name == "" {
		return false
	}
	return recv == owner.Name ||
		strings.HasSuffix(recv, "."+owner.Name) ||
		strings.HasSuffix(recv, "::"+owner.Name)
}
