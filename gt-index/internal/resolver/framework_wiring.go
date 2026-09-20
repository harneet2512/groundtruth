package resolver

// ---------------------------------------------------------------------------
// HAR-90 item 3: framework wiring — MIDDLEWARE_ON + INJECTS edges.
//
// MIDDLEWARE_ON: middleware node -> the node the framework wires it onto.
// Module-level middleware applies to every route registered after it, which a
// line-regex layer cannot enumerate, so the conservative target is the
// file/app anchor (the same convention HANDLES_ROUTE uses). NestJS
// `consumer.apply(M)` and Spring `registry.addInterceptor(new M())` wire onto
// the enclosing module/configurer class instead — that class IS the wiring
// statement's owner, and it is a real graph node.
//
// INJECTS: consumer node -> provider node. Spring `@Autowired`/`@Inject`
// fields and constructor/setter parameters bind the enclosing class to the
// declared type; when the declared type is an interface, IMPLEMENTS edges
// (this pass's own plus taxonomy's DECLARED_IMPLEMENTS already persisted) pick
// the implementation — one impl resolves at 0.6 (name-match resolved impl
// cap), several emit one 0.4 candidate edge each with ambiguous=true in
// metadata. NestJS constructor parameter properties inside a class decorated
// @Injectable/@Controller/@Module/etc. do the same. FastAPI `Depends(f)` is
// emitted in the main relationships pass (handler -> provider function, see
// the language-agnostic DI block in relationships.go).
//
// Layered controller->service->repository wiring is NOT minted as its own
// edge type: the producer's edge vocabulary has no SERVICES/DELEGATES kind.
// Checked: specs.AllTaxonomyEdgeKinds (taxonomy-only set), the callsite-level
// vocabularies in store/sqlite.go (dispatchFormsV2, derivationPassKindsV2 —
// which already names "di_binding" for resolution candidates — and
// callsiteAbstentionVocabulary), and projectionEdgeTypes in
// store/projection.go. The `edges.type` column itself is free text; the
// derivation kind a relationship edge carries is its ResolutionMethod +
// EvidenceType, and that is where the mechanism names below live. Once
// INJECTS binds consumer -> provider type, calls between the two resolve
// through ordinary CALLS — that is the correct carrier for
// controller->service->repository wiring, so no new kind is invented.
// ---------------------------------------------------------------------------

import (
	"bufio"
	"encoding/json"
	"os"
	"regexp"
	"sort"
	"strings"

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// EdgeTypeMiddlewareOn is the MIDDLEWARE_ON edge kind: middleware node ->
// node it is wired onto (file/app anchor, or the enclosing module/configurer
// class for NestJS consumer.apply / Spring addInterceptors).
const EdgeTypeMiddlewareOn = "MIDDLEWARE_ON"

// Framework-wiring mechanisms — carried as the edge's resolution_method /
// evidence_type and as `mechanism` inside the metadata JSON.
const (
	mechExpressUse        = "express_use"
	mechDjangoMiddleware  = "django_middleware"
	mechNestConsumer      = "nest_consumer"
	mechSpringInterceptor = "spring_interceptor"
	mechSpringAutowired   = "spring_autowired"
	mechNestCtor          = "nest_ctor"
	mechFastAPIDepends    = "fastapi_depends"
)

var (
	// Express/Connect middleware registration: `app.use(fn)`, `router.use(fn)`,
	// `app.use(path, fn)`. Anchored at line start — the registration is a
	// statement, not an expression fragment.
	expressUseRe = regexp.MustCompile(`^\s*(?:app|router|server)\.use\s*\(`)

	// Django settings: `MIDDLEWARE = [ 'pkg.mod.Class', ... ]` (also the
	// `MIDDLEWARE += [...]` extension form). Entries are dotted import paths.
	djangoMiddlewareOpenRe  = regexp.MustCompile(`^\s*MIDDLEWARE\s*\+?=\s*[\[\(]`)
	djangoMiddlewareEntryRe = regexp.MustCompile(`["']([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)["']`)

	// NestJS: `consumer.apply(M, ...)` inside a module `configure()` — the
	// receiver is conventionally named *consumer (MiddlewareConsumer). A bare
	// `.apply(` on any other receiver is Function.prototype.apply — NOT
	// middleware registration — so the *consumer tail on the receiver is what
	// makes the call Nest-specific.
	nestConsumerApplyRe = regexp.MustCompile(`\b\w*[Cc]onsumer\.apply\s*\(`)
	// The broken-chain form: a line that is ONLY the consumer identifier
	// (`consumer` / `middlewareConsumer`) is a chain head — the `.apply(` call
	// opens on the next line.
	nestConsumerHeadRe = regexp.MustCompile(`^\w*[Cc]onsumer$`)
	nestDotApplyRe     = regexp.MustCompile(`^\.apply\s*\(`)
	nestForRoutesRe    = regexp.MustCompile(`\.forRoutes\s*\(`)
	// forRoutes route scope: `forRoutes('x')` or `forRoutes({ path: 'x' })`.
	forRoutesObjKeyRe = regexp.MustCompile(`path\s*:\s*["']([^"']+)["']`)

	// Spring interceptor registration inside WebMvcConfigurer.addInterceptors:
	// `registry.addInterceptor(new X())` (also a bare `addInterceptor(X)` ref).
	springAddInterceptorRe = regexp.MustCompile(`\.addInterceptor\s*\(\s*(?:new\s+)?([A-Z]\w*)`)
	springPathPatternsRe   = regexp.MustCompile(`\.addPathPatterns\s*\(\s*["']([^"']+)["']`)

	// Spring/JSR-330 injection annotations.
	springInjectAnnoRe = regexp.MustCompile(`@(?:Autowired|Inject)\b`)

	// Java field declaration: `[modifiers] Type name [=;]`. A `(` terminator is
	// a method/ctor declaration, not a field — the `[=;]` tail excludes it.
	javaFieldDeclRe = regexp.MustCompile(`^\s*(?:(?:private|protected|public|static|final|transient|volatile|abstract|synchronized)\s+)*([A-Z][\w$]*)(?:\s*<[^>]*>)?(?:\[\])?\s+[a-zA-Z_$][\w$]*\s*[=;]`)
	// One Java parameter: `[annotations/modifiers] Type name [= default]`.
	javaParamDeclRe = regexp.MustCompile(`^([A-Z][\w$]*)(?:\s*<[^>]*>)?(?:\[\])?(?:\.\.\.)?\s+[a-zA-Z_$][\w$]*(?:\s*=.*)?$`)
	// Kotlin property: `[modifiers] (var|val) name: Type`.
	kotlinFieldDeclRe = regexp.MustCompile(`^\s*(?:(?:private|protected|public|internal|lateinit|override|const|final)\s+)*(?:var|val)\s+[a-zA-Z_]\w*\s*:\s*([A-Z][\w]*)`)
	// Kotlin constructor declaration opener.
	kotlinCtorRe = regexp.MustCompile(`\bconstructor\s*\(`)
	// Colon-typed parameter: `name: Type` (Kotlin `name: Type`, TS `name: Type`
	// — with any leading modifiers/annotations already stripped).
	colonTypeParamRe = regexp.MustCompile(`[a-zA-Z_$][\w$]*\s*:\s*([A-Z][\w]*)`)

	// TS constructor opener (NestJS parameter-property injection).
	tsCtorDeclRe = regexp.MustCompile(`\bconstructor\s*\(`)
	// Nest decorated-class gate: the annotations that make a class injectable.
	nestDecoratedRe = regexp.MustCompile(`@(Injectable|Controller|Module|Component|Gateway|Resolver)\b`)
	tsClassDeclRe   = regexp.MustCompile(`\bclass\s+([A-Z][\w$]*)`)

	// FastAPI `svc: T = Depends(f)` — the parameter annotation is the declared
	// type metadata while f is the provider the edge targets.
	dependsParamTypeRe = regexp.MustCompile(`\w+\s*:\s*([A-Z]\w*)\s*=\s*Depends\s*\(`)
)

// diFact is one collected dependency-injection site awaiting type resolution.
// The consumer is the containing class node; resolution happens after the
// scan because an interface declared type must hop through the IMPLEMENTS
// edge set, which is complete only once the structural pass has finished.
type diFact struct {
	consumerID   int64
	declaredType string
	file         string
	line         int
	mechanism    string
}

// namedRange is a class-like node with its source span and name — the DI
// emitters need the name for Java constructor matching (`public Foo(Bar b)`)
// and the Nest decorator gate.
type namedRange struct {
	ID    int64
	Name  string
	Start int
	End   int
}

// paramCollect is an unclosed multi-line parameter list being accumulated
// (e.g. `constructor(` whose `)` arrives on a later line).
type paramCollect struct {
	consumerID int64
	mechanism  string
	lang       string
	file       string
	line       int
	deadline   int
	depth      int
	buf        string
}

// pendingWiring is a middleware-registration chain whose route scope rides a
// CHAINED call that may sit on following `.`-leading continuation lines:
// `consumer.apply(M)` awaits `.forRoutes(...)`; `registry.addInterceptor(new
// M())` awaits `.addPathPatterns(...)`.
type pendingWiring struct {
	ids       []int64
	target    int64
	line      int
	deadline  int
	mechanism string
	route     string
}

// frameworkIndexes carries everything the wiring pass needs, prebuilt.
type frameworkIndexes struct {
	classIndex     map[string][]classNodeEntry
	interfaceIndex map[string][]classNodeEntry
	funcFileIndex  map[string]map[string]int64
	fileNodeMap    map[string]int64
	classRanges    map[string][]namedRange
	entryByID      map[int64]classNodeEntry
	implsByIface   map[int64][]classNodeEntry
	fileSet        map[string]bool
}

// edgeEmitFunc matches the addEdgeCounted closure in ResolveRelationships —
// the dedup/stamping rules every relationship edge goes through.
type edgeEmitFunc func(sourceID, targetID int64, edgeType, sourceFile string, sourceLine int, method string, confidence float64, metadata string, candidateCount int)

// resolveFrameworkWiring runs the HAR-90 pass: a second scan over the source
// files (ResolveAPIEdges established that pattern) emitting MIDDLEWARE_ON
// inline and collecting INJECTS sites for post-scan type resolution.
// `inFlight` is this pass's own edge slice — its IMPLEMENTS rows are part of
// the interface->implementation index along with already-persisted IMPLEMENTS
// and taxonomy DECLARED_IMPLEMENTS rows.
func resolveFrameworkWiring(
	db *store.DB,
	files []walker.SourceFile,
	root string,
	classIndex map[string][]classNodeEntry,
	interfaceIndex map[string][]classNodeEntry,
	funcFileIndex map[string]map[string]int64,
	fileNodeMap map[string]int64,
	inFlight []*store.Edge,
	emit edgeEmitFunc,
) {
	idx := frameworkIndexes{
		classIndex:     classIndex,
		interfaceIndex: interfaceIndex,
		funcFileIndex:  funcFileIndex,
		fileNodeMap:    fileNodeMap,
		classRanges:    buildClassRangeIndex(db),
		fileSet:        make(map[string]bool, len(files)),
	}
	idx.entryByID = buildEntryByID(classIndex, interfaceIndex)
	ifaceIDs := make(map[int64]bool)
	for _, entries := range interfaceIndex {
		for _, e := range entries {
			ifaceIDs[e.ID] = true
		}
	}
	idx.implsByIface = buildImplIndex(db, inFlight, idx.entryByID, ifaceIDs)
	for _, sf := range files {
		idx.fileSet[sf.Path] = true
	}

	for _, sf := range files {
		facts := scanFrameworkWiringFile(sf, root, idx, emit)
		emitDIFacts(facts, idx, emit)
	}
}

// scanFrameworkWiringFile scans one source file: emits MIDDLEWARE_ON edges
// inline (they need no post-scan resolution) and returns collected DI facts.
func scanFrameworkWiringFile(sf walker.SourceFile, root string, idx frameworkIndexes, emit edgeEmitFunc) []diFact {
	lang := sf.Language
	if lang != "python" && lang != "javascript" && lang != "typescript" && lang != "java" && lang != "kotlin" {
		return nil
	}
	absPath := sf.AbsPath
	if absPath == "" {
		absPath = root + "/" + sf.Path
	}
	f, err := os.Open(absPath)
	if err != nil {
		return nil
	}
	defer f.Close()

	var facts []diFact
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 1024*1024), 1024*1024)
	lineNum := 0
	pendingInject := false // Spring @Autowired/@Inject awaiting a member decl
	inDjango := false
	djangoDepth := 0
	pendingDeco := false               // TS injectable decorator awaiting its class line
	decorated := make(map[string]bool) // Nest-decorated class names in this file
	consumerHead := false              // previous line was a bare `*consumer` chain head
	var pending *pendingWiring         // apply()/addInterceptor() awaiting its chained scope call
	var collecting *paramCollect       // unclosed parameter list

	// flushPending emits the pending chain's edges and clears it.
	flushPending := func() {
		if pending == nil {
			return
		}
		for _, id := range pending.ids {
			emit(id, pending.target, EdgeTypeMiddlewareOn, sf.Path, pending.line,
				pending.mechanism, 0.9, middlewareEdgeMetadata(pending.mechanism, pending.route), 1)
		}
		pending = nil
	}

	for scanner.Scan() {
		lineNum++
		line := scanner.Text()
		trimmed := strings.TrimSpace(line)

		// 1) Inside an unclosed parameter list — accumulate lines until the
		// depth returns to 0, then extract declared param types.
		if collecting != nil {
			done := false
			for i := 0; i < len(trimmed); i++ {
				switch trimmed[i] {
				case '(', '[', '{':
					collecting.depth++
				case ')', ']', '}':
					collecting.depth--
					if collecting.depth <= 0 {
						collecting.buf += " " + trimmed[:i]
						done = true
						i = len(trimmed) // break out of the char loop
					}
				}
			}
			if !done {
				collecting.buf += " " + trimmed
			}
			if done || lineNum > collecting.deadline {
				for _, tp := range paramTypesFor(collecting.lang, collecting.buf) {
					facts = append(facts, diFact{consumerID: collecting.consumerID, declaredType: tp,
						file: sf.Path, line: collecting.line, mechanism: collecting.mechanism})
				}
				collecting = nil
			}
			continue
		}

		// 2) Comment lines carry no wiring facts — a `// consumer.apply(M)` or
		// `// @Autowired` is prose, not a registration. `*` covers block-comment
		// continuation lines. (`#` is only a Python comment marker; the Python
		// case below additionally ignores `#`-leading list lines.)
		if lang != "python" &&
			(strings.HasPrefix(trimmed, "//") || strings.HasPrefix(trimmed, "/*") || strings.HasPrefix(trimmed, "*")) {
			continue
		}

		// 3) Pending middleware chain: `.`-leading continuation lines may carry
		// the scope call; the first non-continuation line flushes route-less.
		if pending != nil {
			cont := trimmed == "" || strings.HasPrefix(trimmed, ".") ||
				strings.HasPrefix(trimmed, ")") || strings.HasPrefix(trimmed, "//") ||
				strings.HasPrefix(trimmed, "*")
			if !cont {
				flushPending()
				// fall through — this line starts a new statement
			} else {
				flushNow := false
				switch pending.mechanism {
				case mechNestConsumer:
					if loc := nestForRoutesRe.FindStringIndex(trimmed); loc != nil {
						pending.route = forRoutesRoute(trimmed[loc[1]:])
						flushNow = true // forRoutes ends the apply chain
					}
				case mechSpringInterceptor:
					if m := springPathPatternsRe.FindStringSubmatch(trimmed); m != nil {
						pending.route = normalizeMiddlewareRoute(m[1])
						flushNow = true
					}
				}
				if flushNow || lineNum > pending.deadline {
					flushPending()
				}
				continue
			}
		}

		switch lang {
		case "python":
			// Django settings MIDDLEWARE list. Entries are dotted import paths;
			// the last segment names the middleware class.
			if inDjango {
				if !strings.HasPrefix(trimmed, "#") {
					for _, m := range djangoMiddlewareEntryRe.FindAllStringSubmatch(line, -1) {
						emitDjangoMiddleware(m[1], sf, lineNum, idx, emit)
					}
				}
				djangoDepth += strings.Count(line, "[") + strings.Count(line, "(") -
					strings.Count(line, "]") - strings.Count(line, ")")
				if djangoDepth <= 0 {
					inDjango = false
				}
				continue
			}
			if loc := djangoMiddlewareOpenRe.FindStringIndex(line); loc != nil {
				inDjango = true
				rest := line[strings.Index(line, "=")+1:]
				djangoDepth = strings.Count(rest, "[") + strings.Count(rest, "(") -
					strings.Count(rest, "]") - strings.Count(rest, ")")
				for _, m := range djangoMiddlewareEntryRe.FindAllStringSubmatch(line, -1) {
					emitDjangoMiddleware(m[1], sf, lineNum, idx, emit)
				}
				if djangoDepth <= 0 {
					inDjango = false
				}
				continue
			}

		case "javascript", "typescript":
			// Express/Connect middleware: app.use(fn) / app.use(path, fn...).
			if loc := expressUseRe.FindStringIndex(line); loc != nil {
				args := splitArgs(line[loc[1]:])
				route := ""
				start := 0
				if len(args) > 0 {
					first := strings.TrimSpace(args[0])
					if len(first) >= 2 && (first[0] == '"' || first[0] == '\'' || first[0] == '`') && first[len(first)-1] == first[0] {
						route = normalizeMiddlewareRoute(first[1 : len(first)-1])
						start = 1
					}
				}
				for _, a := range args[start:] {
					if tok := cleanRouteHandler(strings.TrimSpace(a)); tok != "" {
						if id := resolveRouteHandler(tok, sf.Path, idx.funcFileIndex, idx.classIndex); id != 0 {
							emit(id, idx.fileNodeMap[sf.Path], EdgeTypeMiddlewareOn, sf.Path, lineNum,
								mechExpressUse, 0.9, middlewareEdgeMetadata(mechExpressUse, route), 1)
						}
					}
				}
			}

			// NestJS decorated-class tracking (the injectable gate for ctor DI).
			if lang == "typescript" {
				if cm := tsClassDeclRe.FindStringSubmatch(trimmed); cm != nil {
					if pendingDeco || nestDecoratedRe.MatchString(trimmed) {
						decorated[cm[1]] = true
					}
					pendingDeco = false
				} else if nestDecoratedRe.MatchString(trimmed) {
					pendingDeco = true
				} else if pendingDeco && trimmed != "" && !strings.HasPrefix(trimmed, "@") &&
					!strings.HasPrefix(trimmed, "//") && !strings.HasPrefix(trimmed, "export") {
					pendingDeco = false
				}
			}

			// NestJS ctor parameter-property injection inside a decorated class.
			if cloc := tsCtorDeclRe.FindStringIndex(trimmed); cloc != nil {
				if cls, ok := enclosingClass(idx.classRanges[sf.Path], lineNum); ok && decorated[cls.Name] {
					rest := trimmed[cloc[1]:] // match ends right after `constructor(`
					content, closed := parenContent(rest)
					if closed {
						for _, tp := range colonParamTypes(content) {
							facts = append(facts, diFact{consumerID: cls.ID, declaredType: tp,
								file: sf.Path, line: lineNum, mechanism: mechNestCtor})
						}
					} else {
						collecting = &paramCollect{consumerID: cls.ID, mechanism: mechNestCtor,
							lang: "typescript", file: sf.Path, line: lineNum,
							deadline: lineNum + 10, depth: 1 + parenDelta(rest), buf: content}
					}
				}
			}

			// NestJS module wiring: consumer.apply(M).forRoutes(...) in
			// configure(). Two surface forms: `consumer.apply(M)` on one line,
			// and the broken chain `consumer` / `.apply(M)` across two.
			applyLoc := nestConsumerApplyRe.FindStringIndex(line)
			if applyLoc == nil && consumerHead {
				if al := nestDotApplyRe.FindStringIndex(trimmed); al != nil {
					lead := len(line) - len(strings.TrimLeft(line, " \t"))
					applyLoc = []int{lead + al[0], lead + al[1]}
				}
			}
			consumerHead = nestConsumerHeadRe.MatchString(trimmed)
			if applyLoc != nil {
				args := splitArgs(line[applyLoc[1]:])
				var ids []int64
				for _, a := range args {
					if tok := cleanRouteHandler(strings.TrimSpace(a)); tok != "" {
						if id := resolveRouteHandler(tok, sf.Path, idx.funcFileIndex, idx.classIndex); id != 0 {
							ids = append(ids, id)
						}
					}
				}
				if len(ids) > 0 {
					target := idx.fileNodeMap[sf.Path]
					if cls, ok := enclosingClass(idx.classRanges[sf.Path], lineNum); ok {
						target = cls.ID // the module class owns the wiring
					}
					// forRoutes may chain on the same line.
					route := ""
					chained := false
					if floc := nestForRoutesRe.FindStringIndex(line[applyLoc[1]:]); floc != nil {
						route = forRoutesRoute(line[applyLoc[1]:][floc[1]:])
						chained = true
					}
					if chained {
						for _, id := range ids {
							emit(id, target, EdgeTypeMiddlewareOn, sf.Path, lineNum,
								mechNestConsumer, 0.9, middlewareEdgeMetadata(mechNestConsumer, route), 1)
						}
					} else {
						pending = &pendingWiring{ids: ids, target: target, line: lineNum,
							deadline: lineNum + 8, mechanism: mechNestConsumer}
					}
				}
			}

		case "java", "kotlin":
			// Spring interceptor registration: registry.addInterceptor(new X()).
			if m := springAddInterceptorRe.FindStringSubmatch(line); m != nil {
				if id := resolveClassNodeSameFileOrUnique(m[1], sf.Path, idx.classIndex); id != 0 {
					target := idx.fileNodeMap[sf.Path]
					if cls, ok := enclosingClass(idx.classRanges[sf.Path], lineNum); ok {
						target = cls.ID // the WebMvcConfigurer owns the registry
					}
					route := ""
					chained := false
					if pm := springPathPatternsRe.FindStringSubmatch(line); pm != nil {
						route = normalizeMiddlewareRoute(pm[1])
						chained = true
					}
					if chained {
						emit(id, target, EdgeTypeMiddlewareOn, sf.Path, lineNum,
							mechSpringInterceptor, 0.9, middlewareEdgeMetadata(mechSpringInterceptor, route), 1)
					} else {
						pending = &pendingWiring{ids: []int64{id}, target: target, line: lineNum,
							deadline: lineNum + 8, mechanism: mechSpringInterceptor}
					}
				}
			}

			// Spring/JSR-330 injection: @Autowired/@Inject member binding.
			if springInjectAnnoRe.MatchString(line) {
				pendingInject = true
				if rest := stripLeadingAnnotations(trimmed); rest != "" {
					// Annotation and member share the line — bind immediately.
					facts = append(facts, bindInjectedMember(rest, lang, sf.Path, lineNum, idx, &collecting)...)
					pendingInject = false
				}
				continue
			}
			if pendingInject {
				if trimmed == "" || strings.HasPrefix(trimmed, "//") || strings.HasPrefix(trimmed, "/*") ||
					strings.HasPrefix(trimmed, "*") || stripLeadingAnnotations(trimmed) == "" {
					continue // blank/comment/stacked-annotation lines keep pending alive
				}
				facts = append(facts, bindInjectedMember(trimmed, lang, sf.Path, lineNum, idx, &collecting)...)
				pendingInject = false
			}
		}
	}
	// Unterminated chains/lists flush honestly at EOF.
	flushPending()
	return facts
}

// emitDjangoMiddleware resolves one MIDDLEWARE dotted path to a middleware
// class node and emits class -> settings-file anchor. The dotted prefix maps
// to a file path first (`a.b.C` -> `a/b.py`); without that hint the name must
// resolve same-file or globally-unique — ambiguity abstains.
func emitDjangoMiddleware(dotted string, sf walker.SourceFile, line int, idx frameworkIndexes, emit edgeEmitFunc) {
	segs := strings.Split(dotted, ".")
	if len(segs) < 2 {
		return
	}
	className := segs[len(segs)-1]
	var id int64
	if hint := strings.Join(segs[:len(segs)-1], "/") + ".py"; idx.fileSet[hint] {
		if e, ok := sameFileMinEntry(idx.classIndex[className], hint); ok {
			id = e.ID
		}
	}
	if id == 0 {
		id = resolveClassNodeSameFileOrUnique(className, sf.Path, idx.classIndex)
	}
	if id != 0 {
		emit(id, idx.fileNodeMap[sf.Path], EdgeTypeMiddlewareOn, sf.Path, line,
			mechDjangoMiddleware, 0.9, middlewareEdgeMetadata(mechDjangoMiddleware, ""), 1)
	}
}

// bindInjectedMember parses the declaration an @Autowired/@Inject annotation
// binds: a field (`Type name [=;]`), a constructor or a method (parameter
// types are the injected dependencies — setter injection is the same
// mechanism). Unclosed parameter lists hand off to `collecting`.
func bindInjectedMember(decl, lang, file string, line int, idx frameworkIndexes, collecting **paramCollect) []diFact {
	cls, ok := enclosingClass(idx.classRanges[file], line)
	if !ok {
		return nil // no containing class node — abstain
	}
	if lang == "kotlin" {
		if ci := strings.Index(decl, "constructor"); ci >= 0 && strings.Contains(decl[ci:], "(") {
			rest := decl[strings.Index(decl[ci:], "(")+ci+1:]
			content, closed := parenContent(rest)
			if !closed {
				*collecting = &paramCollect{consumerID: cls.ID, mechanism: mechSpringAutowired,
					lang: lang, file: file, line: line, deadline: line + 10,
					depth: 1 + parenDelta(rest), buf: content}
				return nil
			}
			return factsForTypes(colonParamTypes(content), cls.ID, file, line)
		}
		if m := kotlinFieldDeclRe.FindStringSubmatch(decl); m != nil {
			return []diFact{{consumerID: cls.ID, declaredType: m[1], file: file, line: line, mechanism: mechSpringAutowired}}
		}
		return nil
	}
	// java: try field first — `Type name [=;]` — because an initializer like
	// `= factory()` carries a `(` that must not reroute the line into a method.
	if m := javaFieldDeclRe.FindStringSubmatch(decl); m != nil {
		return []diFact{{consumerID: cls.ID, declaredType: m[1], file: file, line: line, mechanism: mechSpringAutowired}}
	}
	if pi := strings.Index(decl, "("); pi >= 0 {
		content, closed := parenContent(decl[pi+1:])
		if !closed {
			*collecting = &paramCollect{consumerID: cls.ID, mechanism: mechSpringAutowired,
				lang: lang, file: file, line: line, deadline: line + 10,
				depth: 1 + parenDelta(decl[pi+1:]), buf: content}
			return nil
		}
		return factsForTypes(javaParamTypes(content), cls.ID, file, line)
	}
	return nil
}

func factsForTypes(types []string, consumerID int64, file string, line int) []diFact {
	facts := make([]diFact, 0, len(types))
	for _, tp := range types {
		facts = append(facts, diFact{consumerID: consumerID, declaredType: tp, file: file, line: line, mechanism: mechSpringAutowired})
	}
	return facts
}

// emitDIFacts resolves each collected DI site's declared type and emits
// INJECTS. Resolution: same-file-first, cross-file only when globally unique
// across class AND interface indexes (mixed/ambiguous abstains). An interface
// target hops through IMPLEMENTS: one impl -> 0.6 (name-match impl cap);
// several -> one 0.4 candidate edge each, ambiguous=true in metadata and the
// true candidate count on the edge; none -> the edge lands on the interface
// itself (the injection contract is still the declared fact).
func emitDIFacts(facts []diFact, idx frameworkIndexes, emit edgeEmitFunc) {
	for _, f := range facts {
		id, name, isIface := resolveDeclaredType(f.declaredType, f.file, idx.classIndex, idx.interfaceIndex)
		if id == 0 {
			continue // declared type unresolvable or ambiguous — abstain
		}
		if !isIface {
			emit(f.consumerID, id, "INJECTS", f.file, f.line, f.mechanism, 0.9,
				injectEdgeMetadata(f.mechanism, f.declaredType, name, false), 1)
			continue
		}
		impls := idx.implsByIface[id]
		switch len(impls) {
		case 0:
			emit(f.consumerID, id, "INJECTS", f.file, f.line, f.mechanism, 0.9,
				injectEdgeMetadata(f.mechanism, f.declaredType, name, false), 1)
		case 1:
			emit(f.consumerID, impls[0].ID, "INJECTS", f.file, f.line, f.mechanism, 0.6,
				injectEdgeMetadata(f.mechanism, f.declaredType, impls[0].Name, false), 1)
		default:
			for _, impl := range impls {
				emit(f.consumerID, impl.ID, "INJECTS", f.file, f.line, f.mechanism, 0.4,
					injectEdgeMetadata(f.mechanism, f.declaredType, impl.Name, true), len(impls))
			}
		}
	}
}

// resolveDeclaredType resolves a DI declared type across class and interface
// indexes: exactly one same-file declaration wins; otherwise the name must be
// globally unique across both kinds. Returns (id, nodeName, isInterface);
// id==0 means unresolved OR ambiguous — the caller abstains either way.
func resolveDeclaredType(name, file string, classIndex, interfaceIndex map[string][]classNodeEntry) (int64, string, bool) {
	sameFile := make(map[int64]classNodeEntry)
	all := make(map[int64]classNodeEntry)
	isIfaceID := make(map[int64]bool)
	for _, e := range interfaceIndex[name] {
		all[e.ID] = e
		isIfaceID[e.ID] = true
		if e.FilePath == file {
			sameFile[e.ID] = e
		}
	}
	for _, e := range classIndex[name] {
		all[e.ID] = e
		if e.FilePath == file {
			sameFile[e.ID] = e
		}
	}
	if len(sameFile) == 1 {
		for id, e := range sameFile {
			return id, e.Name, isIfaceID[id]
		}
	}
	if len(sameFile) > 1 || len(all) != 1 {
		return 0, "", false
	}
	for id, e := range all {
		return id, e.Name, isIfaceID[id]
	}
	return 0, "", false
}

// ---------------------------------------------------------------------------
// Index builders
// ---------------------------------------------------------------------------

// buildClassRangeIndex returns file -> class-like node spans (id, name,
// [start,end]) so the DI pass can find the containing class of a field/ctor
// line. The label set matches buildRelationshipIndexes plus the richer
// taxonomy labels (Record/Trait/Protocol).
func buildClassRangeIndex(db *store.DB) map[string][]namedRange {
	out := make(map[string][]namedRange)
	tx, err := db.BeginTx()
	if err != nil {
		return out
	}
	defer tx.Rollback()
	rows, err := tx.Query(`SELECT id, name, file_path, COALESCE(start_line,0), COALESCE(end_line,0)
	  FROM nodes WHERE label IN ('Class','Interface','Struct','Enum','Type','Record','Trait','Protocol')`)
	if err != nil {
		return out
	}
	defer rows.Close()
	for rows.Next() {
		var r namedRange
		var filePath string
		if err := rows.Scan(&r.ID, &r.Name, &filePath, &r.Start, &r.End); err != nil {
			continue
		}
		if r.Start > 0 && r.End >= r.Start {
			out[filePath] = append(out[filePath], r)
		}
	}
	return out
}

// buildEntryByID maps every class/interface node id to its index entry so
// IMPLEMENTS sources recover their name/file/line for metadata + ordering.
func buildEntryByID(classIndex, interfaceIndex map[string][]classNodeEntry) map[int64]classNodeEntry {
	out := make(map[int64]classNodeEntry)
	for _, idx := range []map[string][]classNodeEntry{classIndex, interfaceIndex} {
		for _, entries := range idx {
			for _, e := range entries {
				out[e.ID] = e
			}
		}
	}
	return out
}

// buildImplIndex maps interfaceNodeID -> its implementor entries, from this
// pass's in-flight IMPLEMENTS edges plus already-persisted IMPLEMENTS and
// taxonomy DECLARED_IMPLEMENTS rows (taxonomy DeriveEdges is published before
// pass 4c). An implementor that is itself an interface is excluded — an
// interface is never an injectable implementation.
func buildImplIndex(db *store.DB, inFlight []*store.Edge, entryByID map[int64]classNodeEntry, ifaceIDs map[int64]bool) map[int64][]classNodeEntry {
	out := make(map[int64][]classNodeEntry)
	seen := make(map[[2]int64]bool)
	add := func(srcID, tgtID int64) {
		if srcID == 0 || tgtID == 0 || srcID == tgtID || ifaceIDs[srcID] {
			return
		}
		e, ok := entryByID[srcID]
		if !ok {
			return
		}
		k := [2]int64{srcID, tgtID}
		if seen[k] {
			return
		}
		seen[k] = true
		out[tgtID] = append(out[tgtID], e)
	}
	for _, e := range inFlight {
		if e.Type == "IMPLEMENTS" {
			add(e.SourceID, e.TargetID)
		}
	}
	if tx, err := db.BeginTx(); err == nil {
		rows, qerr := tx.Query(`SELECT source_id, target_id FROM edges WHERE type IN ('IMPLEMENTS','DECLARED_IMPLEMENTS')`)
		if qerr == nil {
			for rows.Next() {
				var s, t int64
				if err := rows.Scan(&s, &t); err == nil {
					add(s, t)
				}
			}
			rows.Close()
		}
		tx.Rollback()
	}
	// Deterministic candidate order: content key, not DB scan order.
	for tgt := range out {
		sort.Slice(out[tgt], func(i, j int) bool { return classEntryLess(out[tgt][i], out[tgt][j]) })
	}
	return out
}

// ---------------------------------------------------------------------------
// Parsing helpers
// ---------------------------------------------------------------------------

// enclosingClass returns the innermost class-like node whose [Start,End]
// range contains `line`, mirroring findEnclosingFunc's ambiguity rule: two
// equally tight enclosing classes resolve to false — never guess an owner.
func enclosingClass(ranges []namedRange, line int) (namedRange, bool) {
	var best namedRange
	bestSpan := -1
	ambiguous := false
	for _, r := range ranges {
		if line < r.Start || line > r.End {
			continue
		}
		span := r.End - r.Start
		switch {
		case bestSpan < 0 || span < bestSpan:
			best, bestSpan, ambiguous = r, span, false
		case span == bestSpan && r.ID != best.ID:
			ambiguous = true
		}
	}
	if ambiguous || bestSpan < 0 {
		return namedRange{}, false
	}
	return best, true
}

// splitArgs scans a fragment that begins INSIDE a call's argument list
// (paren depth 1) and returns the top-level comma-separated arguments,
// stopping at the `)` that closes the list. String literals and generics are
// skipped/counted so their commas never split.
func splitArgs(s string) []string {
	var args []string
	depthParen, depthBracket, depthBrace, depthAngle := 1, 0, 0, 0
	start := 0
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c == '"' || c == '\'' || c == '`' {
			j := i + 1
			for j < len(s) && s[j] != c {
				j++
			}
			i = j
			continue
		}
		switch c {
		case '(':
			depthParen++
		case ')':
			depthParen--
			if depthParen == 0 {
				args = append(args, s[start:i])
				return args
			}
		case '[':
			depthBracket++
		case ']':
			depthBracket--
		case '{':
			depthBrace++
		case '}':
			depthBrace--
		case '<':
			depthAngle++
		case '>':
			if depthAngle > 0 {
				depthAngle--
			}
		case ',':
			if depthParen == 1 && depthBracket == 0 && depthBrace == 0 && depthAngle == 0 {
				args = append(args, s[start:i])
				start = i + 1
			}
		}
	}
	return append(args, s[start:]) // unclosed call — trailing fragment
}

// parenContent returns the content of a fragment that begins INSIDE an open
// paren (depth 1) up to the `)` that closes it. ok=false when the list runs
// past the end of the fragment — the caller accumulates more lines.
func parenContent(s string) (string, bool) {
	depth := 1
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c == '"' || c == '\'' || c == '`' {
			j := i + 1
			for j < len(s) && s[j] != c {
				j++
			}
			i = j
			continue
		}
		switch c {
		case '(', '[', '{':
			depth++
		case ')', ']', '}':
			depth--
			if depth == 0 {
				return s[:i], true
			}
		}
	}
	return s, false
}

// parenDelta is the net open/close balance of `(` vs `)` over a fragment —
// used to seed the depth of a parameter list that did not close on its first
// line.
func parenDelta(s string) int {
	return strings.Count(s, "(") - strings.Count(s, ")")
}

// skipParen returns the text after the balanced `(...)` group s starts with,
// or s unchanged when it does not start with `(`.
func skipParen(s string) string {
	if !strings.HasPrefix(s, "(") {
		return s
	}
	depth := 0
	for i := 0; i < len(s); i++ {
		switch s[i] {
		case '(':
			depth++
		case ')':
			depth--
			if depth == 0 {
				return s[i+1:]
			}
		}
	}
	return s
}

// stripParamAnnotations removes leading `@Name` / `@Name(...)` annotations
// from a parameter fragment, e.g. `@Qualifier("x") ItemService s`.
func stripParamAnnotations(p string) string {
	for {
		p = strings.TrimSpace(p)
		if p == "" || p[0] != '@' {
			return p
		}
		i := 1
		for i < len(p) && (p[i] == '.' || p[i] == '_' || p[i] == '$' ||
			(p[i] >= 'a' && p[i] <= 'z') || (p[i] >= 'A' && p[i] <= 'Z') || (p[i] >= '0' && p[i] <= '9')) {
			i++
		}
		p = skipParen(strings.TrimSpace(p[i:]))
	}
}

// javaParamTypes extracts declared parameter types from a Java parameter-list
// fragment: per top-level param, annotations and `final` are stripped and the
// first capitalized token is the type. Primitives and unparseable params are
// dropped — they are never injected types.
func javaParamTypes(content string) []string {
	var out []string
	for _, p := range splitTopLevel(content) {
		p = stripParamAnnotations(p)
		for {
			p = strings.TrimSpace(p)
			sp := strings.IndexAny(p, " \t")
			if sp < 0 {
				break
			}
			w := p[:sp]
			if w == "final" || w == "volatile" || w == "synchronized" {
				p = p[sp:]
				continue
			}
			break
		}
		if m := javaParamDeclRe.FindStringSubmatch(strings.TrimSpace(p)); m != nil {
			out = append(out, m[1])
		}
	}
	return out
}

// colonParamTypes extracts declared types from colon-typed parameter lists —
// Kotlin `name: Type` and TypeScript `name: Type` (modifiers like
// `private readonly` precede the name and are invisible to the regex).
func colonParamTypes(content string) []string {
	var out []string
	for _, p := range splitTopLevel(content) {
		p = stripParamAnnotations(p)
		if m := colonTypeParamRe.FindStringSubmatch(p); m != nil {
			out = append(out, m[1])
		}
	}
	return out
}

// paramTypesFor dispatches parameter-type extraction by language: Java uses
// `Type name`, Kotlin and TypeScript use `name: Type`.
func paramTypesFor(lang, content string) []string {
	if lang == "java" {
		return javaParamTypes(content)
	}
	return colonParamTypes(content)
}

// forRoutesRoute reads the route scope out of a `.forRoutes(...)` argument
// list: an object `{ path: 'x' }` wins over a bare literal; a controller
// reference (identifier) carries no route — the scope is that controller's
// routes, which reads honestly as null.
func forRoutesRoute(inner string) string {
	args := splitArgs(inner)
	if len(args) == 0 {
		return ""
	}
	first := strings.TrimSpace(args[0])
	if m := forRoutesObjKeyRe.FindStringSubmatch(first); m != nil {
		return normalizeMiddlewareRoute(m[1])
	}
	if len(first) >= 2 && (first[0] == '"' || first[0] == '\'' || first[0] == '`') && first[len(first)-1] == first[0] {
		return normalizeMiddlewareRoute(first[1 : len(first)-1])
	}
	return ""
}

// normalizeMiddlewareRoute normalizes a route-scope literal: absolute paths
// go through normalizePath (same canonicalization as route edges); non-path
// scopes like `'*'` are kept verbatim.
func normalizeMiddlewareRoute(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	if strings.HasPrefix(raw, "/") {
		return normalizePath(raw)
	}
	return raw
}

// middlewareEdgeMetadata serializes {"mechanism":..., "route":<path|null>} —
// null when the middleware applies application-wide.
func middlewareEdgeMetadata(mechanism, route string) string {
	var routeVal any
	if route != "" {
		routeVal = route
	}
	md, err := json.Marshal(map[string]any{"mechanism": mechanism, "route": routeVal})
	if err != nil {
		return ""
	}
	return string(md)
}

// injectEdgeMetadata serializes {"mechanism":..., "declared_type":T,
// "resolved_to":name, "ambiguous":bool} — the DI provenance consumers read.
func injectEdgeMetadata(mechanism, declaredType, resolvedTo string, ambiguous bool) string {
	md, err := json.Marshal(map[string]any{
		"mechanism": mechanism, "declared_type": declaredType,
		"resolved_to": resolvedTo, "ambiguous": ambiguous,
	})
	if err != nil {
		return ""
	}
	return string(md)
}
