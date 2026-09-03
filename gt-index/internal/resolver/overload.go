package resolver

import (
	"path/filepath"
	"strings"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
)

// OverloadNarrowingPass is the pass name under which arity narrowing publishes
// its outcome in a callsite's pass coverage. It is never a pass that can win a
// callsite: narrowing removes candidates, it never derives one.
const OverloadNarrowingPass = "overload_narrowing"

// Narrowing outcome vocabulary. Every callsite carrying more than one candidate
// publishes exactly one of these, so a reader can always tell "narrowing found
// nothing to remove" from "narrowing never ran". A language without binding
// arity gets a named empty result, never silence.
const (
	NarrowingNarrowed             = "narrowed_by_argument_arity"
	NarrowingNoCandidateExcluded  = "no_candidate_excluded"
	NarrowingArityUnknown         = "argument_arity_unknown"
	NarrowingArgumentSpread       = "argument_list_is_spread"
	NarrowingNoProfile            = "no_candidate_parameter_profile"
	NarrowingLanguageNotBinding   = "language_arity_not_binding"
	NarrowingBelowTwoCandidates   = "fewer_than_two_candidates"
	NarrowingWouldExcludeSelected = "would_exclude_selected_target"
	NarrowingWouldExcludeAll      = "would_exclude_every_candidate"
)

// Narrowing pass statuses. "narrowed" is deliberately not "completed_match":
// resolutionCoverage promotes a completed_match pass to the callsite's winning
// pass_kind, and narrowing must never become the mechanism that resolved a
// callsite. It filters a set some mechanism already produced.
const (
	NarrowingStatusNarrowed      = "narrowed"
	NarrowingStatusNotApplicable = "not_applicable"
)

// ArityProfile is the positional-argument shape a definition accepts, derived
// from its declaration header alone. Max is -1 when a variadic parameter makes
// the upper bound unbounded.
type ArityProfile struct {
	Min   int
	Max   int
	Known bool
}

// Accepts reports whether a call with n positional arguments is arity
// compatible. An unknown profile accepts everything: absence of a parsed
// signature is not evidence that a candidate is wrong.
func (p ArityProfile) Accepts(n int) bool {
	if !p.Known {
		return true
	}
	if n < p.Min {
		return false
	}
	return p.Max < 0 || n <= p.Max
}

// OverloadNarrowing is the named result of one narrowing attempt. It is always
// populated: Status and Reason say why an empty narrowing is empty.
type OverloadNarrowing struct {
	Status   string
	Reason   string
	Language string
	Arity    int
	Kept     []int64
	Excluded []int64
}

// arityBindingLanguages are the grammars in which a positional-arity mismatch
// proves a definition cannot be the callee. Everything else stays out and gets
// language_arity_not_binding: JavaScript and Lua discard extra arguments,
// Elm and OCaml curry, Scala has multiple parameter lists and implicits,
// Groovy dispatches dynamically, and the remaining grammars have no calls.
var arityBindingLanguages = map[string]struct{}{
	"c": {}, "cpp": {}, "csharp": {}, "elixir": {}, "go": {}, "java": {},
	"kotlin": {}, "php": {}, "python": {}, "ruby": {}, "rust": {},
	"swift": {}, "typescript": {},
}

// implicitReceiverParams are parameter names a method declares but a call at a
// receiver does not pass. Python's self/cls and Rust's self are spelled in the
// parameter list; Go's receiver sits outside it and Java, C# and TypeScript
// never spell it at all.
var implicitReceiverParams = map[string]map[string]struct{}{
	"python": {"self": {}, "cls": {}, "mcs": {}, "metacls": {}},
	"rust":   {"self": {}},
}

// ArityIsBinding reports whether an arity mismatch in this language is proof
// that a definition cannot be the callee.
func ArityIsBinding(language string) bool {
	_, ok := arityBindingLanguages[language]
	return ok
}

// LanguageForFile names the grammar owning a path through the same extension
// registry the parser uses. It returns "" for a path no grammar claims.
func LanguageForFile(path string) string {
	if path == "" {
		return ""
	}
	spec := specs.ForExtension(strings.ToLower(filepath.Ext(path)))
	if spec == nil {
		return ""
	}
	return spec.Name
}

// ParameterProfile derives the positional arity a declaration accepts. It never
// guesses: a header without a balanced parameter group returns an unknown
// profile, which accepts every arity.
func ParameterProfile(language, signature string) ArityProfile {
	params, ok := declarationParameters(language, signature)
	if !ok {
		return ArityProfile{}
	}
	return profileFromParams(language, params)
}

// ReceiverStrippedProfile is the profile of a method called at a receiver, with
// the leading self/cls parameter removed. Callers accept a candidate when
// either profile matches, so a free function that happens to name its first
// parameter "self" is never wrongly excluded.
func ReceiverStrippedProfile(language, signature string) (ArityProfile, bool) {
	implicit, ok := implicitReceiverParams[language]
	if !ok {
		return ArityProfile{}, false
	}
	params, ok := declarationParameters(language, signature)
	if !ok || len(params) == 0 {
		return ArityProfile{}, false
	}
	if _, isReceiver := implicit[receiverParamName(params[0])]; !isReceiver {
		return ArityProfile{}, false
	}
	return profileFromParams(language, params[1:]), true
}

// receiverParamName reduces a first parameter to the bare name a receiver would
// be spelled with: Rust's "&mut self" and Python's "self: Foo" both reduce to
// "self".
func receiverParamName(param string) string {
	name := strings.TrimSpace(param)
	name = strings.TrimSpace(strings.TrimPrefix(name, "&"))
	name = strings.TrimSpace(strings.TrimPrefix(name, "mut "))
	if idx := strings.IndexAny(name, " :="); idx >= 0 {
		name = name[:idx]
	}
	return strings.TrimSpace(name)
}

func profileFromParams(language string, params []string) ArityProfile {
	profile := ArityProfile{Known: true}
	keywordOnly := false
	for _, raw := range params {
		param := strings.TrimSpace(raw)
		switch {
		case param == "":
			continue
		case param == "/":
			// Python's positional-only marker consumes no argument.
			continue
		case param == "*":
			// Python's keyword-only marker: nothing after it is positional.
			keywordOnly = true
		case isBlockParam(language, param):
			continue
		case isVariadicParam(language, param):
			profile.Max = -1
			return profile
		case keywordOnly:
			continue
		case isOptionalParam(param):
			if profile.Max >= 0 {
				profile.Max++
			}
		default:
			profile.Min++
			if profile.Max >= 0 {
				profile.Max++
			}
		}
	}
	if profile.Max >= 0 && profile.Max < profile.Min {
		profile.Max = profile.Min
	}
	return profile
}

// isVariadicParam recognises the splat spellings across the binding grammars:
// Python "*args"/"**kw", TypeScript "...rest", Go "a ...int", Java
// "String... rest", C++ "Args... args", C# "params object[]", Kotlin "vararg".
func isVariadicParam(language, param string) bool {
	switch {
	case strings.HasPrefix(param, "*"), strings.Contains(param, "..."):
		return true
	case strings.HasPrefix(param, "params "):
		return true
	case language == "kotlin" && strings.HasPrefix(param, "vararg "):
		return true
	}
	return false
}

// isBlockParam skips parameters that consume no positional argument: Ruby's
// block parameter is passed as a block, not in the argument list.
func isBlockParam(language, param string) bool {
	return language == "ruby" && strings.HasPrefix(param, "&")
}

// isOptionalParam reports a parameter a call may omit: a default value, or a
// TypeScript or Swift optional marker on the parameter name.
func isOptionalParam(param string) bool {
	if strings.Contains(stripNestedGroups(param), "=") {
		return true
	}
	name := param
	if idx := strings.Index(param, ":"); idx > 0 {
		name = param[:idx]
	}
	return strings.HasSuffix(strings.TrimSpace(name), "?")
}

// stripNestedGroups blanks out nested groups so a default inside a generic or a
// call is not mistaken for this parameter's own default.
func stripNestedGroups(param string) string {
	var out strings.Builder
	depth := 0
	for i := 0; i < len(param); i++ {
		switch param[i] {
		case '(', '[', '{':
			depth++
		case ')', ']', '}':
			if depth > 0 {
				depth--
			}
		default:
			if depth == 0 {
				out.WriteByte(param[i])
			}
		}
	}
	return out.String()
}

// declarationParameters extracts the top-level parameter list from a
// declaration header. It reports false when the header has no balanced
// parameter group, which keeps an unparsed signature from narrowing anything.
func declarationParameters(language, signature string) ([]string, bool) {
	sig := strings.TrimSpace(signature)
	if sig == "" {
		return nil, false
	}
	start := 0
	if language == "go" && strings.HasPrefix(sig, "func (") {
		// The receiver group precedes the parameter group. Skip it, and treat a
		// receiver-only header as unparsable rather than reading the receiver
		// as parameters.
		_, end, ok := balancedGroup(sig, strings.Index(sig, "("))
		if !ok {
			return nil, false
		}
		start = end + 1
	}
	open := strings.Index(sig[start:], "(")
	if open < 0 {
		return nil, false
	}
	inner, _, ok := balancedGroup(sig, start+open)
	if !ok {
		return nil, false
	}
	if strings.TrimSpace(inner) == "" {
		return nil, true
	}
	return splitTopLevel(inner), true
}

// balancedGroup returns the contents of the parenthesised group opening at
// open, and the index of its closing parenthesis.
func balancedGroup(text string, open int) (inner string, closeAt int, ok bool) {
	if open < 0 || open >= len(text) || text[open] != '(' {
		return "", 0, false
	}
	depth := 0
	var quote byte
	for i := open; i < len(text); i++ {
		ch := text[i]
		if quote != 0 {
			if ch == '\\' {
				i++
				continue
			}
			if ch == quote {
				quote = 0
			}
			continue
		}
		switch ch {
		case '"', '\'', '`':
			quote = ch
		case '(', '[', '{':
			depth++
		case ')', ']', '}':
			depth--
			if depth == 0 {
				return text[open+1 : i], i, true
			}
			if depth < 0 {
				return "", 0, false
			}
		}
	}
	return "", 0, false
}

// splitTopLevel splits a parameter list on commas that are not inside a nested
// group, a string, or a generic argument list.
func splitTopLevel(inner string) []string {
	var params []string
	depth, angle, start := 0, 0, 0
	var quote byte
	for i := 0; i < len(inner); i++ {
		ch := inner[i]
		if quote != 0 {
			if ch == '\\' {
				i++
				continue
			}
			if ch == quote {
				quote = 0
			}
			continue
		}
		switch ch {
		case '"', '\'', '`':
			quote = ch
		case '(', '[', '{':
			depth++
		case ')', ']', '}':
			depth--
		case '<':
			if i > 0 && isIdentByte(inner[i-1]) {
				angle++
			}
		case '>':
			if angle > 0 {
				angle--
			}
		case ',':
			if depth == 0 && angle == 0 {
				params = append(params, inner[start:i])
				start = i + 1
			}
		}
	}
	return append(params, inner[start:])
}

func isIdentByte(b byte) bool {
	return b == '_' || b == '>' ||
		(b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z') || (b >= '0' && b <= '9')
}

// NarrowByArity removes candidates whose declared parameter list cannot accept
// the callsite's argument count. It is a filter over a set another mechanism
// already produced: it never selects a target, never changes a mechanism, and
// never raises evidence. A set narrowed to one candidate is exactly as
// unverified as the set it came from.
//
// selected, when non-nil, is a target the resolver already chose under evidence
// narrowing cannot see. Narrowing abstains rather than contradict it.
func NarrowByArity(callsiteLanguage string, arity *uint16, spread bool, candidates []int64, selected *int64, meta map[int64]NodeMeta) OverloadNarrowing {
	result := OverloadNarrowing{
		Status:   NarrowingStatusNotApplicable,
		Language: callsiteLanguage,
		Arity:    -1,
		Kept:     candidates,
	}
	switch {
	case len(candidates) < 2:
		result.Reason = NarrowingBelowTwoCandidates
		return result
	case !ArityIsBinding(callsiteLanguage):
		result.Reason = NarrowingLanguageNotBinding
		return result
	case arity == nil:
		result.Reason = NarrowingArityUnknown
		return result
	case spread:
		result.Reason = NarrowingArgumentSpread
		return result
	}
	observed := int(*arity)
	result.Arity = observed

	kept, excluded := partitionByArity(observed, candidates, meta)
	switch {
	case countProfiled(candidates, meta) == 0:
		result.Reason = NarrowingNoProfile
		return result
	case len(excluded) == 0:
		result.Reason = NarrowingNoCandidateExcluded
		return result
	case len(kept) == 0:
		// Every candidate disagrees with the observed arity. That is evidence
		// the signatures or the argument count were misread, not evidence the
		// callsite has no target. Publish the disagreement and keep the set.
		result.Reason = NarrowingWouldExcludeAll
		return result
	case selected != nil && !containsID(kept, *selected):
		result.Reason = NarrowingWouldExcludeSelected
		return result
	}
	result.Status = NarrowingStatusNarrowed
	result.Reason = NarrowingNarrowed
	result.Kept, result.Excluded = kept, excluded
	return result
}

// partitionByArity splits candidates into those arity-compatible with observed
// and those provably not, preserving the incoming order in both.
func partitionByArity(observed int, candidates []int64, meta map[int64]NodeMeta) (kept, excluded []int64) {
	for _, id := range candidates {
		if candidateAcceptsArity(observed, id, meta) {
			kept = append(kept, id)
			continue
		}
		excluded = append(excluded, id)
	}
	return kept, excluded
}

// countProfiled reports how many candidates have a parsed parameter profile at
// all. Zero means narrowing had nothing to reason over, which is a different
// statement from "narrowing removed nothing".
func countProfiled(candidates []int64, meta map[int64]NodeMeta) int {
	profiled := 0
	for _, id := range candidates {
		m, ok := meta[id]
		if !ok {
			continue
		}
		language := LanguageForFile(m.File)
		if ArityIsBinding(language) && ParameterProfile(language, m.Signature).Known {
			profiled++
		}
	}
	return profiled
}

// candidateAcceptsArity is deliberately permissive at every unknown: a
// candidate is excluded only when its own grammar makes arity binding and its
// parsed profile rejects the observed count under every receiver reading.
func candidateAcceptsArity(observed int, id int64, meta map[int64]NodeMeta) bool {
	m, ok := meta[id]
	if !ok {
		return true
	}
	language := LanguageForFile(m.File)
	if !ArityIsBinding(language) {
		return true
	}
	if ParameterProfile(language, m.Signature).Accepts(observed) {
		return true
	}
	if stripped, ok := ReceiverStrippedProfile(language, m.Signature); ok {
		return stripped.Accepts(observed)
	}
	return false
}

func containsID(ids []int64, want int64) bool {
	for _, id := range ids {
		if id == want {
			return true
		}
	}
	return false
}
