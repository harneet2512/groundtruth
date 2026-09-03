package resolver

import "testing"

// Overload narrowing is a filter over a candidate set some other mechanism
// produced. These tests pin the two properties that make it safe to publish:
// it removes only candidates the argument count provably cannot reach, and it
// never converts the survivors into better evidence than they arrived with.

func narrowingMeta() map[int64]NodeMeta {
	return map[int64]NodeMeta{
		1: {Name: "handle", File: "app/one.py", Signature: "def handle(self, request):"},
		2: {Name: "handle", File: "app/two.py", Signature: "def handle(self, request, retries, backoff):"},
		3: {Name: "handle", File: "app/three.py", Signature: "def handle(self, request, *rest):"},
	}
}

func arity(n uint16) *uint16 { return &n }

func TestArityNarrowingRemovesCandidatesTheArgumentCountCannotReach(t *testing.T) {
	// One positional argument at the callsite; `self` is not passed. Only the
	// one-parameter and the variadic definition can accept it.
	got := NarrowByArity("python", arity(1), false, []int64{1, 2, 3}, nil, narrowingMeta())
	if got.Status != NarrowingStatusNarrowed || got.Reason != NarrowingNarrowed {
		t.Fatalf("narrowing did not fire: %+v", got)
	}
	if len(got.Kept) != 2 || got.Kept[0] != 1 || got.Kept[1] != 3 {
		t.Fatalf("wrong candidates kept: %v", got.Kept)
	}
	if len(got.Excluded) != 1 || got.Excluded[0] != 2 {
		t.Fatalf("wrong candidates excluded: %v", got.Excluded)
	}
}

func TestArityNarrowingNeverPromotesTheSetItShrank(t *testing.T) {
	// A source-supported mechanism with a single surviving candidate is exactly
	// the shape sourceSupportedResolution accepts. Narrowing having produced it
	// must not be enough: the callsite was ambiguous before the filter ran.
	narrowed := ResolvedCall{
		Method: "import", EvidenceType: "ast_import",
		CandidateNodeIDs: []int64{7}, ArityNarrowedFrom: 3,
	}
	if sourceSupportedResolution(narrowed) {
		t.Fatal("narrowing promoted an ambiguous callsite to source-supported evidence")
	}
	// The same callsite that was never narrowed keeps whatever its mechanism grants.
	untouched := narrowed
	untouched.ArityNarrowedFrom = 0
	if !sourceSupportedResolution(untouched) {
		t.Fatal("the no-narrowing case lost the tier its mechanism grants")
	}
}

func TestArityNarrowingNamesEveryEmptyResult(t *testing.T) {
	jsMeta := map[int64]NodeMeta{
		1: {File: "a.js", Signature: "function handle(request) {"},
		2: {File: "b.js", Signature: "function handle(request, retries) {"},
	}
	cases := []struct {
		name       string
		language   string
		arity      *uint16
		spread     bool
		candidates []int64
		meta       map[int64]NodeMeta
		wantReason string
	}{
		{"javascript discards extra arguments", "javascript", arity(1), false, []int64{1, 2}, jsMeta, NarrowingLanguageNotBinding},
		{"no argument list on the callsite", "python", nil, false, []int64{1, 2, 3}, narrowingMeta(), NarrowingArityUnknown},
		{"spread makes the count a lower bound", "python", arity(1), true, []int64{1, 2, 3}, narrowingMeta(), NarrowingArgumentSpread},
		{"nothing to narrow", "python", arity(1), false, []int64{1}, narrowingMeta(), NarrowingBelowTwoCandidates},
		{"no signature to reason over", "python", arity(1), false, []int64{8, 9}, map[int64]NodeMeta{8: {File: "a.py"}, 9: {File: "b.py"}}, NarrowingNoProfile},
		{"every candidate accepts the count", "python", arity(1), false, []int64{1, 3}, narrowingMeta(), NarrowingNoCandidateExcluded},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := NarrowByArity(tc.language, tc.arity, tc.spread, tc.candidates, nil, tc.meta)
			if got.Reason != tc.wantReason {
				t.Fatalf("reason %q, want %q", got.Reason, tc.wantReason)
			}
			if got.Status != NarrowingStatusNotApplicable {
				t.Fatalf("status %q, want %q", got.Status, NarrowingStatusNotApplicable)
			}
			if len(got.Kept) != len(tc.candidates) {
				t.Fatalf("a non-applicable narrowing dropped candidates: %v", got.Kept)
			}
		})
	}
}

func TestArityNarrowingAbstainsRatherThanEmptyTheSetOrOverrideTheResolver(t *testing.T) {
	// Nine arguments match nothing. That is evidence the signatures or the
	// count were misread, not evidence the callsite has no target.
	all := NarrowByArity("python", arity(9), false, []int64{1, 2}, nil, narrowingMeta())
	if all.Reason != NarrowingWouldExcludeAll || len(all.Kept) != 2 {
		t.Fatalf("narrowing emptied the candidate set: %+v", all)
	}
	// The resolver already chose candidate 2 under evidence narrowing cannot
	// see. Narrowing abstains instead of contradicting it.
	protected := int64(2)
	guarded := NarrowByArity("python", arity(1), false, []int64{1, 2, 3}, &protected, narrowingMeta())
	if guarded.Reason != NarrowingWouldExcludeSelected || len(guarded.Kept) != 3 {
		t.Fatalf("narrowing excluded the resolver's own target: %+v", guarded)
	}
}

func TestParameterProfilesAcrossTheBindingGrammars(t *testing.T) {
	cases := []struct {
		language  string
		signature string
		min, max  int
	}{
		{"go", "func (s *Server) Handle(req Request, opts ...Option) error", 1, -1},
		{"go", "func Handle(req Request, timeout time.Duration) error", 2, 2},
		{"python", "def handle(request, retries=3):", 1, 2},
		{"python", "def handle(request, *, verbose=False):", 1, 1},
		{"typescript", "function handle(request: Request, retries?: number): void", 1, 2},
		{"typescript", "handle(request: Request, ...rest: Option[])", 1, -1},
		{"java", "public void handle(Request request, String... tags)", 1, -1},
		{"csharp", "public void Handle(Request request, params object[] tags)", 1, -1},
		{"kotlin", "fun handle(request: Request, vararg tags: String)", 1, -1},
		{"ruby", "def handle(request, retries = 3, &block)", 1, 2},
		{"rust", "fn handle(&self, request: Request) -> Result<(), Error>", 2, 2},
		{"php", "function handle($request, $retries = 3)", 1, 2},
		{"java", "public void handle()", 0, 0},
	}
	for _, tc := range cases {
		got := ParameterProfile(tc.language, tc.signature)
		if !got.Known || got.Min != tc.min || got.Max != tc.max {
			t.Errorf("%s %q: profile %+v, want min=%d max=%d", tc.language, tc.signature, got, tc.min, tc.max)
		}
	}
}

func TestAnUnparsableHeaderAcceptsEveryArity(t *testing.T) {
	unknown := ParameterProfile("python", "def handle")
	if unknown.Known {
		t.Fatal("a header with no parameter group claimed a known profile")
	}
	if !unknown.Accepts(0) || !unknown.Accepts(9) {
		t.Fatal("an unknown profile excluded a candidate")
	}
}

func TestImplicitReceiverIsNotAPositionalArgument(t *testing.T) {
	// `self` is spelled in the declaration but never passed at `obj.handle(x)`.
	// Both readings are offered so a free function that names its first
	// parameter self is not wrongly excluded.
	full := ParameterProfile("python", "def handle(self, request):")
	if full.Min != 2 {
		t.Fatalf("declared profile lost a parameter: %+v", full)
	}
	stripped, ok := ReceiverStrippedProfile("python", "def handle(self, request):")
	if !ok || stripped.Min != 1 || stripped.Max != 1 {
		t.Fatalf("receiver-stripped profile %+v (ok=%v)", stripped, ok)
	}
	if _, ok := ReceiverStrippedProfile("python", "def handle(request):"); ok {
		t.Fatal("a first parameter that is not a receiver was stripped anyway")
	}
	if rustStripped, ok := ReceiverStrippedProfile("rust", "fn handle(&mut self, request: Request)"); !ok || rustStripped.Min != 1 {
		t.Fatalf("rust receiver not recognised: %+v (ok=%v)", rustStripped, ok)
	}
}

func TestArityBindingIsDecidedPerGrammarNotGuessed(t *testing.T) {
	for _, language := range []string{"python", "go", "java", "typescript", "rust", "csharp", "kotlin", "ruby", "php", "swift", "elixir", "c", "cpp"} {
		if !ArityIsBinding(language) {
			t.Errorf("%s should make an arity mismatch binding", language)
		}
	}
	for _, language := range []string{"javascript", "lua", "elm", "ocaml", "scala", "groovy", "yaml", "markdown", ""} {
		if ArityIsBinding(language) {
			t.Errorf("%s must not narrow on arity", language)
		}
	}
}

func TestLanguageIsReadFromTheSameExtensionRegistryTheParserUses(t *testing.T) {
	cases := map[string]string{
		"a/b.py": "python", "a/b.ts": "typescript", "a/b.go": "go",
		"a/b.rs": "rust", "a/b.js": "javascript", "a/b.unknownext": "", "": "",
	}
	for path, want := range cases {
		if got := LanguageForFile(path); got != want {
			t.Errorf("LanguageForFile(%q) = %q, want %q", path, got, want)
		}
	}
}
